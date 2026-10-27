"""Linux process identity, systemd instance units and optional X11 controls."""
from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys

import psutil


CHROME_NAMES = {'chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome-headless-shell'}


def chrome_root(process, args: list[str]) -> bool:
    return process.name().lower() in CHROME_NAMES and not any(a.startswith('--type=') for a in args)


def matches_profile(process, directory: str, port: int, created: float | None = None) -> bool:
    try:
        args = process.cmdline()
        return (chrome_root(process, args) and _profile_directory_matches(args, directory, process.pid)
                and f'--remote-debugging-port={port}' in args
                and (created is None or abs(process.create_time() - created) < 0.1))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _profile_directory_matches(args: list[str], directory: str, pid: int) -> bool:
    indices = [i for i, a in enumerate(args) if a.startswith('--user-data-dir=')]
    if len(indices) != 1 or not Path(directory).is_absolute():
        return False
    index = indices[0]
    value = args[index].split('=', 1)[1]
    if Path(value).is_absolute() and Path(value).resolve() == Path(directory).resolve():
        return True
    # Chrome rewrites its Linux process title after relaunch. psutil then splits
    # that title on spaces, losing original argv boundaries (psutil issue 1179).
    # Reconstruct only the known exact switch, and require independent proof
    # that this PID owns the requested directory; never adopt a prefix match.
    if sys.platform != 'linux':
        return False
    expected = f'--user-data-dir={directory}'.split(' ')
    if len(expected) < 2 or args[index:index + len(expected)] != expected:
        return False
    try:
        lock = os.readlink(Path(directory) / 'SingletonLock')
        hostname, owner = lock.rsplit('-', 1)
        return hostname == socket.gethostname() and owner == str(pid)
    except (OSError, ValueError):
        return False


def unit_name(profile_id: int) -> str:
    prefix = os.environ.get('CHROME_MANAGER_UNIT_PREFIX', 'chromemanager')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', prefix):
        raise ValueError('Invalid systemd unit prefix')
    return f'{prefix}-browser@{int(profile_id)}.service'


def systemctl(*args: str, timeout: float = 30) -> str:
    result = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise OSError(f'systemctl {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout.strip()


class SystemdProcess:
    """Popen-compatible identity handle; the instance unit owns its cgroup."""
    def __init__(self, profile_id: int):
        self.unit = unit_name(profile_id)
        systemctl('start', self.unit)
        self.pid = int(systemctl('show', self.unit, '--property=MainPID', '--value'))
        if not self.pid:
            raise OSError(f'{self.unit} did not report a main process')
        self.process = psutil.Process(self.pid)

    def poll(self):
        return None if self.process.is_running() else 0

    def terminate(self):
        systemctl('stop', self.unit)

    def kill(self):
        systemctl('kill', '--signal=SIGKILL', self.unit)

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)


def close_process(pid: int) -> int:
    psutil.Process(pid).send_signal(signal.SIGTERM)
    return 1


def focus_window(pid: int) -> None:
    """Only X11 with wmctrl; never operate an arbitrary desktop window."""
    if not os.environ.get('DISPLAY') or not shutil.which('wmctrl'):
        raise OSError('前台激活需要 X11 DISPLAY 和 wmctrl；无头/Wayland 模式请使用远程桌面')
    result = subprocess.run(['wmctrl', '-lp'], capture_output=True, text=True, timeout=3, check=True)
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 3 and parts[2] == str(pid):
            subprocess.run(['wmctrl', '-ia', parts[0]], timeout=3, check=True)
            return
    raise OSError('没有找到该实例的 X11 浏览器窗口')
