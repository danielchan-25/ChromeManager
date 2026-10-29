"""Keep managed process state current without collecting resource metrics."""

from __future__ import annotations

import logging
from threading import Event, Lock, Thread
import time

from chrome_manager.core.process_manager import ProcessManager
from chrome_manager.utils.audit import operation


class StatusSynchronizer:
    """Reconcile browser lifecycle state; retain only the last check's outcome."""

    def __init__(self, database, profiles, settings=None):
        self.profiles = profiles
        self.processes = ProcessManager(database, settings.shutdown_timeout if settings else 10, settings)
        self.lock = Lock()
        self.checked_at = 0.0
        self.warning = None
        self.stop_event = Event()
        self.thread = None

    def refresh(self):
        with operation('instances.synchronize', level=logging.DEBUG):
            self.processes.reconcile()
            self.processes.recover_restarted(self.profiles.list())
        with self.lock:
            self.checked_at = time.time()
            self.warning = None

    def snapshot(self):
        with self.lock:
            return {'checked_at': self.checked_at, 'warning': self.warning}

    def record_error(self):
        with self.lock:
            self.warning = '实例状态更新失败，正在重试'
        logging.getLogger('chrome_manager.lifecycle').exception(self.warning)

    def start(self):
        if self.thread is not None:
            return
        try:
            self.refresh()
        except Exception:
            self.record_error()

        def synchronize():
            while not self.stop_event.wait(10):
                try:
                    self.refresh()
                except Exception:
                    self.record_error()

        self.thread = Thread(target=synchronize, name='chrome-manager-instance-status', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)
