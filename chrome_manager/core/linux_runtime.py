"""Linux process identity, systemd instance units and optional X11 controls."""
from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import signal
import subprocess

import psutil


CHROME_NAMES = {'chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome-headless-shell'}


def chrome_root(process, args: list[str]) -> bool:
    return process.name().lower() in CHROME_NAMES and not any(a.startswith('--type=') for a in args)


def matches_profile(process, directory: str, port: int, created: float | None = None) -> bool:
    try:
        args = process.cmdline()
        dirs = [a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir=')]
        return (chrome_root(process, args) and len(dirs) == 1 and Path(dirs[0]).is_absolute()
                and Path(dirs[0]).resolve() == Path(directory).resolve()
                and f'--remote-debugging-port={port}' in args
                and (created is None or abs(process.create_time() - created) < 0.1))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
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
