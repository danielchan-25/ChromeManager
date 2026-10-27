"""Headed Chrome startup with CDP readiness verification."""

from __future__ import annotations

import logging
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
from chrome_manager.core.cdp_ports import browser_port
from chrome_manager.core.linux_runtime import SystemdProcess
from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile
from chrome_manager.utils.audit import event, operation, step
from logging.handlers import RotatingFileHandler


class ChromeStartError(RuntimeError):
    """Raised when a profile's visible Chrome window cannot become CDP-ready."""


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name == 'posix':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        """Start an isolated Linux Chrome process and wait for verified CDP."""
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
        internal_port = browser_port(self.database, profile, True)
        with step('browser.start.port_check'):
            if not is_port_available(internal_port):
                raise ChromeStartError(f"CDP 端口 {profile.cdp_port} 已被其他程序占用")

        arguments = self.arguments(profile, internal_port)
        event('browser.start.arguments', arguments=arguments)
        with step('browser.start.claim'), self.database.transaction() as connection:
            claimed = connection.execute("UPDATE profiles SET status = 'starting' WHERE id = ? AND status = 'stopped' AND is_deleted = 0", (profile.id,))
            if claimed.rowcount != 1:
                raise ChromeStartError("实例状态已改变，请刷新后重试")
            connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?,'chrome_start','Starting Linux Chrome')", (profile.id,))

        process = None
        try:
            log_path, native_path = self.prepare_logs(profile)
            with step('browser.start.spawn') as result:
                if self.settings.runtime == 'systemd':
                    process = SystemdProcess(profile.id)
                elif self.settings.runtime == 'direct':
                    with log_path.open('ab', buffering=0) as output:
                        output.write(f'\n=== ChromeManager Linux start profile={profile.id} port={profile.cdp_port} ===\n'.encode())
                        process = subprocess.Popen(arguments, stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                                   start_new_session=True,
                                                   env={**os.environ, 'CHROME_LOG_FILE': str(native_path)})
                else:
                    raise ChromeStartError('process.runtime 必须为 direct 或 systemd')
                result['browser_pid'] = process.pid
                created_at = psutil.Process(process.pid).create_time()
            with step('browser.start.wait_for_cdp', browser_pid=process.pid):
                self._wait_for_cdp(internal_port, self.settings.startup_timeout, process.pid)
        except Exception as exc:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                        process.kill()
                        process.wait(timeout=5)
            finally:
                with self.database.transaction() as connection:
                    connection.execute("UPDATE profiles SET status='stopped' WHERE id=?", (profile.id,))
                    connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?,'chrome_start_failed',?)", (profile.id, f'{type(exc).__name__}: {exc}'))
            logging.getLogger('chrome_manager.lifecycle').exception('Linux Chrome 启动失败：实例 %s', profile.id)
            raise ChromeStartError(f'Chrome 启动失败：{exc}') from exc

        with step('browser.start.record_running', browser_pid=process.pid), self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
            connection.execute("INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port,started_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)", (profile.id, process.pid, created_at, internal_port))
            connection.execute("UPDATE cdp_ports SET last_pid=?,last_started_at=CURRENT_TIMESTAMP WHERE profile_id=?", (process.pid, profile.id))
            connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?,'chrome_started',?)", (profile.id, f'Linux Chrome started with PID {process.pid}'))
        return process.pid

    def arguments(self, profile: Profile, internal_port: int) -> list[str]:
        chrome_path = self._find_chrome()
        if os.name == 'posix' and self.settings.runtime != 'systemd' and not self.settings.headless and not os.environ.get('DISPLAY'):
            raise ChromeStartError('有界面模式需要 DISPLAY；请启动固定显示服务，或配置 browser.headless=true')
        if os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0 and self.settings.sandbox:
            raise ChromeStartError('请以普通用户运行 Chrome；不要以 root 启动生产服务')
        extension_path = ProfileInfoExtension.prepare(profile)
        arguments = [
            str(chrome_path),
            f"--user-data-dir={profile.user_data_dir}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={internal_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--start-minimized",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--enable-logging",
            f"--log-file={self.settings.data_root / 'logs' / 'profiles' / str(profile.id) / 'chrome_debug.log'}",
            f"--load-extension={extension_path}",
        ]
        if profile.proxy_url:
            arguments.append(f"--proxy-server={profile.proxy_url}")
        else:
            # Do not inherit the Windows/system proxy when this Profile has no explicit proxy.
            arguments.append("--no-proxy-server")
        if self.settings.headless:
            arguments.append('--headless=new')
        if not self.settings.sandbox:
            arguments.append('--no-sandbox')
        arguments.append(f"http://127.0.0.1:{self.web_port}/profiles/{quote(profile.name, safe='')}")
        if profile.default_url:
            arguments.extend(url.strip() for url in profile.default_url.split(",") if url.strip())
        return arguments


    def prepare_logs(self, profile: Profile) -> tuple[Path, Path]:
        log_path = self.settings.data_root / 'logs' / 'profiles' / str(profile.id) / 'chrome.log'
        native_path = log_path.with_name('chrome_debug.log')
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (log_path, native_path):
            rotation = RotatingFileHandler(path, maxBytes=self.settings.log_max_size_mb * 1024 * 1024,
                                          backupCount=self.settings.log_backup_count, encoding='utf-8')
            try:
                if path.stat().st_size >= rotation.maxBytes or (path == native_path and path.stat().st_size > 0):
                    rotation.doRollover()
            finally:
                rotation.close()
        return log_path, native_path

    def _find_chrome(self) -> Path:
        candidates = []
        if self.settings.chrome_path:
            configured = Path(self.settings.chrome_path).expanduser()
            if configured.is_absolute():
                candidates.append(configured)
            elif resolved := shutil.which(self.settings.chrome_path):
                candidates.append(Path(resolved))
            if not candidates or not candidates[0].is_file():
                raise ChromeStartError(f'配置的 Chrome 可执行文件不存在：{self.settings.chrome_path}')
            return candidates[0]
        for command in ('google-chrome-stable', 'google-chrome', 'chromium', 'chromium-browser'):
            if found := shutil.which(command):
                candidates.append(Path(found))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ChromeStartError('未找到 Linux Chrome，请安装 Chrome Stable 或配置 browser.chrome_path')

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
