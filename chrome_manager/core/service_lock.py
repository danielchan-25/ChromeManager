"""Process-held lock; a crash releases it without leaving a stale PID lock."""

import os
from contextlib import contextmanager
from pathlib import Path


class ServiceAlreadyRunning(RuntimeError):
    pass


@contextmanager
def service_lock(data_root: Path):
    data_root.mkdir(parents=True, exist_ok=True)
    # Never unlink this file: another process may already have opened the inode.
    with (data_root / 'manager.lock').open('a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ServiceAlreadyRunning('此数据目录已有管理服务运行，请使用现有控制台') from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
