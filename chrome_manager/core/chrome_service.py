"""Headed Chrome startup with CDP readiness verification."""

from __future__ import annotations

import json
import logging
import ctypes
import socket
import subprocess
import time
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener

import psutil

from chrome_manager.config import Settings
from chrome_manager.core.profile_info_extension import ProfileInfoExtension
from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile


class ChromeStartError(RuntimeError):
    """Raised when a profile's visible Chrome window cannot become CDP-ready."""


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
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
        """Start a normal, visible Chrome window and wait until CDP responds."""
        if profile.status != "stopped":
            raise ChromeStartError(f"Profile「{profile.name}」当前状态为 {profile.status}，无法启动")
        if profile.cdp_port is None:
            raise ChromeStartError("Profile 没有分配 CDP 端口")
        if not is_port_available(profile.cdp_port):
            raise ChromeStartError(f"CDP 端口 {profile.cdp_port} 已被其他程序占用")

        chrome_path = self._find_chrome()
        extension_path = ProfileInfoExtension.prepare(profile)
        arguments = [
            str(chrome_path),
            f"--user-data-dir={profile.user_data_dir}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={profile.cdp_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--start-minimized",
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
        with self.database.transaction() as connection:
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
            process = subprocess.Popen(arguments)
            created_at = psutil.Process(process.pid).create_time()
            self._wait_for_cdp(profile.cdp_port, self.settings.startup_timeout)
        except (ChromeStartError, OSError, psutil.Error) as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            with self.database.transaction() as connection:
                connection.execute("UPDATE profiles SET status = 'stopped' WHERE id = ?", (profile.id,))
                connection.execute(
                    "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_start_failed', ?)",
                    (profile.id, "Chrome did not become CDP-ready"),
                )
            logging.getLogger('chrome_manager.lifecycle').exception('实例 %s 启动失败', profile.id)
            raise ChromeStartError("Chrome 启动失败，请检查 Chrome 路径、端口与错误日志后重试") from exc

        with self.database.transaction() as connection:
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
        candidates = [Path(self.settings.chrome_path)] if self.settings.chrome_path else []
        candidates.extend([
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ChromeStartError("未找到 Google Chrome，请配置 browser.chrome_path")

    @staticmethod
    def _wait_for_cdp(port: int, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        opener = build_opener(ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=1) as response:  # noqa: S310 - localhost CDP only
                    payload = json.load(response)
                if all(field in payload for field in ("Browser", "Protocol-Version", "webSocketDebuggerUrl")):
                    return
            except (OSError, HTTPException, json.JSONDecodeError):
                time.sleep(0.25)
        raise ChromeStartError(f"Chrome 在 {timeout} 秒内未能通过 CDP 端口 {port} 就绪")
