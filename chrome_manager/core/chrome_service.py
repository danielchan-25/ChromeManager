"""Headed Chrome startup with CDP readiness verification."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

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
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

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
            f"--load-extension={extension_path}",
        ]
        if profile.proxy_url:
            arguments.append(f"--proxy-server={profile.proxy_url}")
        else:
            # Do not inherit the Windows/system proxy when this Profile has no explicit proxy.
            arguments.append("--no-proxy-server")
        arguments.append(f"http://127.0.0.1:8765/profiles/{quote(profile.name, safe='')}")
        if profile.default_url:
            arguments.extend(url.strip() for url in profile.default_url.split(",") if url.strip())
        with self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status = 'starting' WHERE id = ?", (profile.id,))
            connection.execute(
                "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_start', ?)",
                (profile.id, "Starting headed Chrome window"),
            )

        # No CREATE_NO_WINDOW / DETACHED_PROCESS flags: this is intentionally a visible Windows Chrome window.
        process = subprocess.Popen(arguments)
        try:
            self._wait_for_cdp(profile.cdp_port, self.settings.startup_timeout)
        except ChromeStartError:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            with self.database.transaction() as connection:
                connection.execute("UPDATE profiles SET status = 'failed' WHERE id = ?", (profile.id,))
                connection.execute(
                    "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_start_failed', ?)",
                    (profile.id, "Chrome did not become CDP-ready"),
                )
            raise

        with self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status = 'running' WHERE id = ?", (profile.id,))
            connection.execute(
                "INSERT INTO runtime_sessions(profile_id, pid, process_create_time, cdp_port, started_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (profile.id, process.pid, time.time(), profile.cdp_port),
            )
            connection.execute(
                "UPDATE cdp_ports SET last_pid = ?, last_started_at = CURRENT_TIMESTAMP WHERE profile_id = ?",
                (process.pid, profile.id),
            )
            connection.execute(
                "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_started', ?)",
                (profile.id, f"Visible Chrome started with PID {process.pid}"),
            )
        return process.pid

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
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=1) as response:  # noqa: S310 - localhost CDP only
                    payload = json.load(response)
                if all(field in payload for field in ("Browser", "Protocol-Version", "webSocketDebuggerUrl")):
                    return
            except (URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(0.25)
        raise ChromeStartError(f"Chrome 在 {timeout} 秒内未能通过 CDP 端口 {port} 就绪")
