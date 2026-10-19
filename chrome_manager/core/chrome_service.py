"""Headed Chrome startup with CDP readiness verification."""

from __future__ import annotations

import logging
import ctypes
import os
import shutil
import socket
import subprocess
import time
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote

import psutil
import websocket

from chrome_manager.config import DEFAULT_DATA_ROOT, Settings
from chrome_manager.core.profile_info_extension import ProfileInfoExtension
from chrome_manager.core.cdp import probe_browser
from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile
from chrome_manager.utils.audit import event, operation, step
from logging.handlers import RotatingFileHandler


class ChromeStartError(RuntimeError):
    """Raised when a profile's visible Chrome window cannot become CDP-ready."""


def is_port_available(port: int) -> bool:
    # A persistent wildcard Windows forward is expected, but a real loopback
    # listener still means that Chrome (or another process) owns this port.
    forwarded = False
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp') as key:
                forwarded = winreg.QueryValueEx(key, f'0.0.0.0/{port}')[0] == f'127.0.0.1/{port}'
        except OSError:
            pass
    if forwarded:
        if any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
               and c.laddr.ip in {'127.0.0.1', '::1'} for c in psutil.net_connections('tcp')):
            return False
        # The elevated synchronizer removes stopped profiles on its next polling cycle.
        # Windows may otherwise let the wildcard forward prevent Chrome binding.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r'SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp') as key:
                    winreg.QueryValueEx(key, f'0.0.0.0/{port}')
            except OSError:
                break
            time.sleep(0.25)
        else:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class ChromeService:
    def __init__(self, database: Database, settings: Settings, web_port: int = 8765) -> None:
        self.database = database
        self.settings = settings
        self.web_port = web_port

    def start(self, profile: Profile) -> int:
        """Start a normal, visible Chrome window and wait until CDP responds."""
        with operation('browser.start', profile_id=profile.id, profile_name=profile.name,
                       port=profile.cdp_port, user_data_dir=profile.user_data_dir) as outcome:
            pid = self._start(profile)
            outcome['browser_pid'] = pid
            return pid

    def _start(self, profile: Profile) -> int:
        event('browser.start.validate', status=profile.status)
        if profile.status != "stopped":
            raise ChromeStartError(f"Profile「{profile.name}」当前状态为 {profile.status}，无法启动")
        if profile.cdp_port is None:
            raise ChromeStartError("Profile 没有分配 CDP 端口")
        with step('browser.start.port_check'):
            if not is_port_available(profile.cdp_port):
                raise ChromeStartError(f"CDP 端口 {profile.cdp_port} 已被其他程序占用")

        with step('browser.start.find_executable') as result:
            chrome_path = self._find_chrome()
            result['chrome_path'] = str(chrome_path)
        with step('browser.start.prepare_extension') as result:
            extension_path = ProfileInfoExtension.prepare(profile)
            result['extension_path'] = str(extension_path)
        arguments = [
            str(chrome_path),
            f"--user-data-dir={profile.user_data_dir}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={profile.cdp_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--start-minimized",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--enable-logging",
            f"--load-extension={extension_path}",
        ]
        if profile.proxy_url:
            arguments.append(f"--proxy-server={profile.proxy_url}")
        else:
            # Do not inherit the Windows/system proxy when this Profile has no explicit proxy.
            arguments.append("--no-proxy-server")
        arguments.append(f"http://127.0.0.1:{self.web_port}/profiles/{quote(profile.name, safe='')}")
        if profile.default_url:
            arguments.extend(url.strip() for url in profile.default_url.split(",") if url.strip())
        event('browser.start.arguments', arguments=arguments)
        with step('browser.start.claim'), self.database.transaction() as connection:
            claimed = connection.execute("UPDATE profiles SET status = 'starting' WHERE id = ? AND status = 'stopped' AND is_deleted = 0", (profile.id,))
            if claimed.rowcount != 1:
                raise ChromeStartError("实例状态已改变，请刷新后重试")
            connection.execute(
                "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_start', ?)",
                (profile.id, "Starting headed Chrome window"),
            )

        # No CREATE_NO_WINDOW / DETACHED_PROCESS flags: this is intentionally a visible Windows Chrome window.
        process = None
        try:
            with step('browser.start.capture_output') as result:
                log_path = self.settings.data_root / 'logs' / 'profiles' / str(profile.id) / 'chrome.log'
                native_path = log_path.with_name('chrome_debug.log')
                log_path.parent.mkdir(parents=True, exist_ok=True)
                # Chrome writes directly to this handle; rotate only before launching.
                for path in (log_path, native_path):
                    rotation = RotatingFileHandler(path, maxBytes=self.settings.log_max_size_mb * 1024 * 1024,
                                                   backupCount=self.settings.log_backup_count, encoding='utf-8')
                    try:
                        size = path.stat().st_size
                        # Chrome overwrites its native file each launch; preserve the previous run.
                        if size >= rotation.maxBytes or (path == native_path and size > 0):
                            rotation.doRollover()
                    finally:
                        rotation.close()
                result['chrome_log'] = str(log_path)
                result['chrome_debug_log'] = str(native_path)
            with step('browser.start.spawn') as result:
                with log_path.open('ab', buffering=0) as output:
                    output.write(f'\n=== ChromeManager start profile={profile.id} port={profile.cdp_port} '
                                 f'time={time.strftime("%Y-%m-%d %H:%M:%S")} ===\n'.encode('utf-8'))
                    process = subprocess.Popen(arguments, stdout=output, stderr=output,
                                               env={**os.environ, 'CHROME_LOG_FILE': str(native_path)})
                result['browser_pid'] = process.pid
                created_at = psutil.Process(process.pid).create_time()
                result['process_create_time'] = created_at
            with step('browser.start.wait_for_cdp', browser_pid=process.pid):
                self._wait_for_cdp(profile.cdp_port, self.settings.startup_timeout, process.pid)
        except Exception as exc:
            try:
                with step('browser.start.cleanup', browser_pid=getattr(process, 'pid', None)):
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            event('browser.start.cleanup.kill', level=logging.WARNING, browser_pid=process.pid)
                            process.kill()
                            process.wait(timeout=3)
            finally:
                with step('browser.start.rollback'), self.database.transaction() as connection:
                    connection.execute("UPDATE profiles SET status = 'stopped' WHERE id = ?", (profile.id,))
                    connection.execute(
                        "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_start_failed', ?)",
                        (profile.id, f"{type(exc).__name__}: {exc}"),
                    )
            logging.getLogger('chrome_manager.lifecycle').exception('实例 %s 启动失败', profile.id)
            raise ChromeStartError("Chrome 启动失败，请检查 Chrome 路径、端口与错误日志后重试") from exc

        with step('browser.start.record_running', browser_pid=process.pid), self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status = 'running' WHERE id = ?", (profile.id,))
            connection.execute(
                "INSERT INTO runtime_sessions(profile_id, pid, process_create_time, cdp_port, started_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (profile.id, process.pid, created_at, profile.cdp_port),
            )
            connection.execute(
                "UPDATE cdp_ports SET last_pid = ?, last_started_at = CURRENT_TIMESTAMP WHERE profile_id = ?",
                (process.pid, profile.id),
            )
            connection.execute(
                "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_started', ?)",
                (profile.id, f"Visible Chrome started with PID {process.pid}"),
            )
        try:
            with step('browser.start.minimize', browser_pid=process.pid):
                self._minimize_windows(process.pid)
        except (OSError, AttributeError):
            logging.getLogger('chrome_manager.lifecycle').exception('实例 %s 已启动，但最小化失败', profile.id)
        logging.getLogger('chrome_manager.lifecycle').info('实例 %s 已启动，PID=%s', profile.id, process.pid)
        return process.pid

    @staticmethod
    def _minimize_windows(pid: int) -> None:
        """Minimize only visible windows owned by the newly started Chrome tree."""
        try:
            root = psutil.Process(pid)
            process_ids = {root.pid, *(child.pid for child in root.children(recursive=True))}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return

        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def minimize_window(handle: int, _: int) -> bool:
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            if process_id.value in process_ids and user32.IsWindowVisible(handle):
                user32.ShowWindow(handle, 6)  # SW_MINIMIZE
            return True

        user32.EnumWindows(minimize_window, 0)

    def _find_chrome(self) -> Path:
        candidates = []
        if self.settings.chrome_path:
            configured = Path(self.settings.chrome_path).expanduser()
            candidates.append(configured if configured.is_absolute() else DEFAULT_DATA_ROOT.parent / configured)
        for variable, suffix in (
            ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
            ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        ):
            if base := os.environ.get(variable):
                candidates.append(Path(base) / suffix)
        if chrome_on_path := shutil.which("chrome.exe"):
            candidates.append(Path(chrome_on_path))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ChromeStartError("未找到 Google Chrome，请配置 browser.chrome_path")

    @staticmethod
    def _wait_for_cdp(port: int, timeout: int, expected_pid: int | None = None) -> None:
        deadline = time.monotonic() + timeout
        attempt = 0
        last_error = None
        while time.monotonic() < deadline:
            attempt += 1
            event('browser.start.cdp_attempt', port=port, browser_pid=expected_pid, attempt=attempt)
            try:
                probe_browser(port, min(2, max(0.001, deadline - time.monotonic())), expected_pid)
                return
            except (OSError, HTTPException, ValueError, KeyError, TypeError, websocket.WebSocketException) as exc:
                last_error = exc
                event('browser.start.cdp_retry', port=port, browser_pid=expected_pid,
                      attempt=attempt, error_type=type(exc).__name__, error=str(exc), exc_info=True)
                time.sleep(0.25)
        raise ChromeStartError(f"Chrome 在 {timeout} 秒内未能通过 CDP 端口 {port} 就绪；"
                               f"尝试 {attempt} 次，最后错误：{last_error}") from last_error
