import platform

import gevent
from gevent.lock import BoundedSemaphore
from sqlalchemy.exc import OperationalError
import psutil

from inbox.providers import providers
from inbox.config import config
from inbox.contacts.remote_sync import ContactSync
from inbox.events.remote_sync import EventSync, GoogleEventSync
from inbox.heartbeat.status import clear_heartbeat_status
from nylas.logging import get_logger
from nylas.logging.sentry import log_uncaught_errors
from inbox.models.session import session_scope
from inbox.models import Account
from inbox.scheduling.queue import QueueClient
from inbox.util.concurrency import retry_with_logging
from inbox.util.stats import statsd_client

from inbox.mailsync.backends import module_registry

USE_GOOGLE_PUSH_NOTIFICATIONS = \
    'GOOGLE_PUSH_NOTIFICATIONS' in config.get('FEATURE_FLAGS', [])


class SyncService(object):
    """
    Parameters
    ----------
    process_identifier: string
        Unique identifying string for this process (currently
        <hostname>:<process_number>)
    cpu_id: int
        If a system has 4 cores, value from 0-3. (Each sync service on the
        system should get a different value.)
    poll_interval : int
        Seconds between polls for account changes.
    """
    def __init__(self, process_identifier, cpu_id, poll_interval=10):
        self.host = platform.node()
        self.cpu_id = cpu_id
        self.process_identifier = process_identifier
        self.monitor_cls_for = {mod.PROVIDER: getattr(
            mod, mod.SYNC_MONITOR_CLS) for mod in module_registry.values()
            if hasattr(mod, 'SYNC_MONITOR_CLS')}

        for p_name, p in providers.iteritems():
            if p_name not in self.monitor_cls_for:
                self.monitor_cls_for[p_name] = self.monitor_cls_for["generic"]

        self.log = get_logger()
        self.log.bind(cpu_id=cpu_id)
        self.log.info('starting mail sync process',
                      supported_providers=module_registry.keys())

        self.syncing_accounts = set()
        self.email_sync_monitors = {}
        self.contact_sync_monitors = {}
        self.event_sync_monitors = {}
        self.poll_interval = poll_interval
        self.semaphore = BoundedSemaphore(1)

        self.stealing_enabled = config.get('SYNC_STEAL_ACCOUNTS', True)
        self.zone = config.get('ZONE')
        self.queue_client = QueueClient(self.zone)

        # We call cpu_percent in a non-blocking way. Because of the way
        # this function works, it'll always return 0.0 the first time
        # we call it. See: https://pythonhosted.org/psutil/#psutil.cpu_percent
        # for more details.
        psutil.cpu_percent(percpu=True)

    def run(self):
        retry_with_logging(self._run_impl, self.log)

    def _run_impl(self):
        """
        Polls for newly registered accounts and checks for start/stop commands.

        """
        while True:
            self.poll()
            gevent.sleep(self.poll_interval)

    def poll(self):
        # We really don't want to take on more load than we can bear, so we need
        # to check the CPU usage before accepting new accounts.
        # Note that we can't check this for the current core because the kernel
        # transparently moves programs across cores.
        usage_per_cpu = psutil.cpu_percent(percpu=True)

        # Conservatively, stop accepting accounts if the CPU usage is over 90%
        # for every core.
        overloaded_cpus = all([cpu_usage > 90.0 for cpu_usage in usage_per_cpu])

        if self.stealing_enabled and not overloaded_cpus:
            r = self.queue_client.claim_next(self.process_identifier)
            if r:
                self.log.info('Claimed new account sync', account_id=r)

        # Determine which accounts to sync
        start_accounts = self.accounts_to_sync()
        statsd_client.gauge(
            'accounts.{}.mailsync-{}.count'.format(self.host, self.cpu_id),
            len(start_accounts))

        # Perform the appropriate action on each account
        for account_id in start_accounts:
            if account_id not in self.syncing_accounts:
                try:
                    self.start_sync(account_id)
                except OperationalError:
                    self.log.error('Database error starting account sync',
                                   exc_info=True)
                    log_uncaught_errors()

        stop_accounts = self.syncing_accounts - set(start_accounts)
        for account_id in stop_accounts:
            self.log.info('sync service stopping sync',
                          account_id=account_id)
            try:
                self.stop_sync(account_id)
            except OperationalError:
                self.log.error('Database error stopping account sync',
                               exc_info=True)
                log_uncaught_errors()

    def accounts_to_sync(self):
        return {int(k) for k, v in self.queue_client.assigned().items()
                if v == self.process_identifier}

    def start_sync(self, account_id):
        """
        Starts a sync for the account with the given account_id.
        If that account doesn't exist, does nothing.

        """
        with self.semaphore, session_scope(account_id) as db_session:
            acc = db_session.query(Account).get(account_id)
            if acc is None:
                self.log.error('no such account', account_id=account_id)
                return
            self.log.info('starting sync', account_id=acc.id,
                          email_address=acc.email_address)

            if acc.id not in self.syncing_accounts:
                try:
                    acc.sync_host = self.process_identifier
                    if acc.sync_email:
                        monitor = self.monitor_cls_for[acc.provider](acc)
                        self.email_sync_monitors[acc.id] = monitor
                        monitor.start()

                    info = acc.provider_info
                    if info.get('contacts', None) and acc.sync_contacts:
                        contact_sync = ContactSync(acc.email_address,
                                                   acc.verbose_provider,
                                                   acc.id,
                                                   acc.namespace.id)
                        self.contact_sync_monitors[acc.id] = contact_sync
                        contact_sync.start()

                    if info.get('events', None) and acc.sync_events:
                        if (USE_GOOGLE_PUSH_NOTIFICATIONS and
                                acc.provider == 'gmail'):
                            event_sync = GoogleEventSync(acc.email_address,
                                                         acc.verbose_provider,
                                                         acc.id,
                                                         acc.namespace.id)
                        else:
                            event_sync = EventSync(acc.email_address,
                                                   acc.verbose_provider,
                                                   acc.id,
                                                   acc.namespace.id)
                        self.event_sync_monitors[acc.id] = event_sync
                        event_sync.start()

                    acc.sync_started()
                    self.syncing_accounts.add(acc.id)
                    db_session.commit()
                    self.log.info('Sync started', account_id=account_id,
                                  sync_host=acc.sync_host)
                except Exception:
                    self.log.error('Error starting sync', exc_info=True,
                                   account_id=account_id)
            else:
                self.log.info('sync already started', account_id=account_id)

    def stop_sync(self, account_id):
        """
        Stops the sync for the account with given account_id.
        If that account doesn't exist, does nothing.

        """

        with self.semaphore:
            self.log.info('Stopping monitors', account_id=account_id)
            if account_id in self.email_sync_monitors:
                self.email_sync_monitors[account_id].kill()
                del self.email_sync_monitors[account_id]

            # Stop contacts sync if necessary
            if account_id in self.contact_sync_monitors:
                self.contact_sync_monitors[account_id].kill()
                del self.contact_sync_monitors[account_id]

            # Stop events sync if necessary
            if account_id in self.event_sync_monitors:
                self.event_sync_monitors[account_id].kill()
                del self.event_sync_monitors[account_id]

            self.syncing_accounts.discard(account_id)

            # Update database/heartbeat state
            with session_scope(account_id) as db_session:
                acc = db_session.query(Account).get(account_id)
                if not acc.sync_should_run:
                    clear_heartbeat_status(acc.id)
                if acc.sync_stopped(self.process_identifier):
                    self.log.info('sync stopped', account_id=account_id)

            r = self.queue_client.unassign(account_id, self.process_identifier)
            return r
