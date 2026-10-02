"""Bounded, read-only connection and renderer checks for managed CDP instances."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from threading import Event, Lock, Thread
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

import websocket
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from chrome_manager.db.database import Database


class CdpMonitor:
    def __init__(self, database: Database, *, interval: int = 60, timeout_ms: int = 8000) -> None:
        self.database = database
        self.interval = interval
        self.timeout_ms = timeout_ms
        self._states: dict[int, dict[str, object]] = {}
        self._error: str | None = None
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    def states(self) -> dict[int, dict[str, object]]:
        with self._lock:
            result = {profile_id: state.copy() for profile_id, state in self._states.items()}
        cutoff = time.time() - (2 * self.interval + self.timeout_ms / 1000)
        for state in result.values():
            if state["checked_at"] < cutoff:
                state["state"] = "failed"
                state["reason"] = "CDP 健康检查结果已过期"
        return result

    def error(self) -> str | None:
        with self._lock:
            return self._error

    def _running(self) -> list[tuple[int, int, int]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT p.id, c.port, r.pid FROM profiles p "
                "JOIN cdp_ports c ON c.id = p.cdp_port_id "
                "JOIN runtime_sessions r ON r.profile_id = p.id AND r.stopped_at IS NULL "
                "WHERE p.status = 'running' AND p.is_deleted = 0 ORDER BY p.id"
            ).fetchall()
        return [(int(row["id"]), int(row["port"]), int(row["pid"])) for row in rows if row["pid"]]

    @staticmethod
    def _page_label(url: str) -> str:
        parsed = urlsplit(url)
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:200]

    @staticmethod
    def _targets(port: int, timeout: float) -> list[dict[str, object]]:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as response:
            return [target for target in json.load(response) if target.get("type") == "page"]

    @staticmethod
    def _probe_page(target: dict[str, object], timeout: float) -> dict[str, object] | None:
        """Use a separate session so one stuck renderer cannot block diagnosis of the others."""
        connection = None
        deadline = time.monotonic() + timeout
        try:
            connection = websocket.create_connection(
                str(target["webSocketDebuggerUrl"]), timeout=timeout, suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost", "::1"],
            )
            pending = {1, 2}
            connection.send(json.dumps({"id": 1, "method": "Page.getFrameTree", "params": {}}))
            connection.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
                "expression": "1", "returnByValue": True, "timeout": max(1, int(timeout * 1000)),
            }}))
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise websocket.WebSocketTimeoutException("page response timed out")
                connection.settimeout(remaining)
                message = json.loads(connection.recv())
                if message.get("id") not in pending:
                    continue
                if "error" in message or "exceptionDetails" in message.get("result", {}):
                    raise ValueError("page command failed")
                pending.remove(message["id"])
            return None
        except websocket.WebSocketBadStatusException as exc:
            if exc.status_code == 404:
                return None  # A task may have closed its tab while the snapshot was being checked.
            reason = "页面 CDP 连接失败"
        except (websocket.WebSocketTimeoutException, TimeoutError):
            reason = "页面 CDP 响应超时"
        except (OSError, websocket.WebSocketException, ValueError, KeyError):
            reason = "页面 CDP 命令失败"
        finally:
            if connection is not None:
                connection.close(timeout=0)
        return {"id": target.get("id"), "url": CdpMonitor._page_label(str(target.get("url", ""))),
                "reason": reason}

    async def _probe_pages(self, port: int, deadline: float) -> list[dict[str, object]]:
        timeout = min(2.0, max(0.001, deadline - time.monotonic()))
        targets = await asyncio.to_thread(self._targets, port, timeout)
        timeout = min(2.0, max(0.001, deadline - time.monotonic()))
        results = await asyncio.gather(*(asyncio.to_thread(self._probe_page, target, timeout) for target in targets))
        failed = [result for result in results if result is not None]
        if failed:
            remaining = min(2.0, max(0.001, deadline - time.monotonic()))
            live_ids = {target.get("id") for target in await asyncio.to_thread(self._targets, port, remaining)}
            failed = [result for result in failed if result["id"] in live_ids]
        return failed

    async def _check(self, browser_type, profile_id: int, port: int, pid: int) -> tuple[int, dict[str, object]]:
        started = time.monotonic()
        failed_pages: list[dict[str, object]] = []
        try:
            deadline = started + self.timeout_ms / 1000
            failed_pages = await asyncio.wait_for(self._probe_pages(port, deadline), self.timeout_ms / 1000)
            if failed_pages:
                state = "failed"
                reason = "；".join(f"{page['reason']}：{page['url']}" for page in failed_pages)
            else:
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                await browser_type.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=remaining)
                state = "ok"
                reason = ""
        except PlaywrightTimeoutError:
            state = "failed"
            reason = "Playwright 连接超时"
        except asyncio.TimeoutError:
            state = "failed"
            reason = "页面 CDP 健康检查超时"
        except Exception as exc:
            state = "failed"
            reason = f"CDP 健康检查失败：{type(exc).__name__}"
        result: dict[str, object] = {
            "state": state, "reason": reason, "pid": pid, "port": port,
            "checked_at": int(time.time()), "latency_ms": round((time.monotonic() - started) * 1000),
            "failed_pages": failed_pages,
        }
        return profile_id, result

    def check_once(self) -> dict[int, dict[str, object]]:
        running = self._running()
        if not running:
            with self._lock:
                self._states = {}
                self._error = None
            return {}

        async def check_all() -> list[tuple[int, dict[str, object]]]:
            async with async_playwright() as playwright:
                return await asyncio.gather(
                    *(self._check(playwright.chromium, profile_id, port, pid) for profile_id, port, pid in running)
                )

        results = dict(asyncio.run(check_all()))
        with self._lock:
            previous = self._states
            self._states = results
            self._error = None
        for profile_id, result in results.items():
            if result["state"] == "failed" and (
                previous.get(profile_id, {}).get("state") != "failed"
                or previous.get(profile_id, {}).get("pid") != result["pid"]
                or previous.get(profile_id, {}).get("reason") != result["reason"]
            ):
                logging.getLogger("chrome_manager.cdp").warning(
                    "实例 %s 的 CDP 不可用：端口 %s，%s", profile_id, result["port"], result["reason"]
                )
            if result["state"] == "ok" and previous.get(profile_id, {}).get("state") == "failed":
                logging.getLogger("chrome_manager.cdp").info("实例 %s 的 CDP 已恢复", profile_id)
        return results

    def start(self) -> None:
        if self._thread is not None:
            return

        def collect() -> None:
            while not self._stop.is_set():
                try:
                    self.check_once()
                except Exception:
                    with self._lock:
                        self._error = "CDP 健康检查自身失败，下轮重试"
                    logging.getLogger("chrome_manager.cdp").exception("CDP 健康检查失败，下轮重试")
                if self._stop.wait(self.interval):
                    break

        self._thread = Thread(target=collect, name="chrome-manager-cdp-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout_ms / 1000 + 2)
