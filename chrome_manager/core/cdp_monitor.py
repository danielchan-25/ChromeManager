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

from chrome_manager.core.cdp import local_endpoint, probe_browser

from chrome_manager.db.database import Database
from chrome_manager.utils.audit import event, operation, step


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
        with step('cdp.http.targets', failure_level=logging.DEBUG, port=port) as result:
            with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as response:
                targets = [target for target in json.load(response) if target.get("type") == "page"]
            result['page_count'] = len(targets)
            return targets

    @staticmethod
    def _probe_page(target: dict[str, object], timeout: float) -> dict[str, object] | None:
        """Use a separate session so one stuck renderer cannot block diagnosis of the others."""
        connection = None
        deadline = time.monotonic() + timeout
        started = time.perf_counter()
        details = {'target_id': target.get('id'), 'url': CdpMonitor._page_label(str(target.get('url', ''))),
                   'timeout_seconds': timeout}
        event('cdp.page.begin', **details)
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
                if not isinstance(message, dict) or message.get("id") not in pending:
                    continue
                if ("error" in message or not isinstance(message.get('result'), dict)
                        or "exceptionDetails" in message['result']):
                    raise ValueError(f'page command {message.get("id")} failed: '
                                     f'{message.get("error") or (message["result"].get("exceptionDetails", "invalid result") if isinstance(message.get("result"), dict) else "invalid result")}')
                pending.remove(message["id"])
                event('cdp.page.command.completed', command_id=message['id'],
                      method='Page.getFrameTree' if message['id'] == 1 else 'Runtime.evaluate', **details)
            event('cdp.page.completed', duration_ms=round((time.perf_counter() - started) * 1000, 2), **details)
            return None
        except websocket.WebSocketBadStatusException as exc:
            if exc.status_code == 404:
                event('cdp.page.closed_during_check', **details)
                return None  # A task may have closed its tab while the snapshot was being checked.
            reason = "页面 CDP 连接失败"
            event('cdp.page.failed', level=logging.WARNING, error_type=type(exc).__name__,
                  error=str(exc), exc_info=True, **details)
        except (websocket.WebSocketTimeoutException, TimeoutError) as exc:
            reason = "页面 CDP 响应超时"
            event('cdp.page.failed', level=logging.WARNING, error_type=type(exc).__name__,
                  error=str(exc), exc_info=True, **details)
        except (OSError, websocket.WebSocketException, ValueError, KeyError, TypeError) as exc:
            reason = "页面 CDP 命令失败"
            event('cdp.page.failed', level=logging.WARNING, error_type=type(exc).__name__,
                  error=str(exc), exc_info=True, **details)
        finally:
            if connection is not None:
                with step('cdp.page.socket.close', failure_level=logging.WARNING, **details):
                    connection.close(timeout=0)
        return {"id": target.get("id"), "url": CdpMonitor._page_label(str(target.get("url", ""))),
                "reason": reason}

    async def _probe_pages(self, port: int, deadline: float) -> list[dict[str, object]]:
        timeout = min(2.0, max(0.001, deadline - time.monotonic()))
        targets = await asyncio.to_thread(self._targets, port, timeout)
        limit = asyncio.Semaphore(8)

        async def probe(target):
            async with limit:
                timeout = min(3.0, deadline - time.monotonic())
                if timeout <= 0:
                    return {'id': target.get('id'), 'url': self._page_label(str(target.get('url', ''))),
                            'reason': '页面 CDP 健康检查超时'}
                if target.get('webSocketDebuggerUrl'):
                    local_endpoint(str(target['webSocketDebuggerUrl']), port)
                return await asyncio.to_thread(self._probe_page, target, timeout)

        results = await asyncio.gather(*(probe(target) for target in targets))
        failed = [result for result in results if result is not None]
        if failed:
            remaining = min(2.0, max(0.001, deadline - time.monotonic()))
            live_ids = {target.get("id") for target in await asyncio.to_thread(self._targets, port, remaining)}
            failed = [result for result in failed if result["id"] in live_ids]
        return failed

    async def _check(self, profile_id: int, port: int, pid: int) -> tuple[int, dict[str, object]]:
        with operation('cdp.health_check', level=logging.DEBUG, profile_id=profile_id, port=port,
                       browser_pid=pid) as outcome:
            result = await self._check_instance(profile_id, port, pid)
            outcome.update(state=result[1]['state'], reason=result[1]['reason'],
                           failed_pages=result[1]['failed_pages'])
            return result

    async def _check_instance(self, profile_id: int, port: int, pid: int) -> tuple[int, dict[str, object]]:
        started = time.monotonic()
        failed_pages: list[dict[str, object]] = []
        try:
            deadline = started + self.timeout_ms / 1000
            for attempt in (1, 2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('CDP health check deadline exceeded')
                # A single 2s budget includes HTTP, two WebSocket handshakes and PID
                # verification. Brief scheduling delays must get one fresh probe,
                # while a wrong PID or malformed response still fails immediately.
                budget = min(2 if attempt == 1 else 4, remaining)
                try:
                    await asyncio.wait_for(asyncio.to_thread(probe_browser, port, budget, pid), remaining)
                    if attempt > 1:
                        event('cdp.health_check.retry_recovered', level=logging.INFO, attempt=attempt)
                    break
                except (TimeoutError, websocket.WebSocketTimeoutException) as exc:
                    if attempt == 2 or deadline - time.monotonic() < 0.5:
                        raise
                    event('cdp.health_check.retry', level=logging.WARNING, attempt=attempt,
                          next_attempt=attempt + 1, error_type=type(exc).__name__, error=str(exc),
                          remaining_ms=round((deadline - time.monotonic()) * 1000), exc_info=True)
                    await asyncio.sleep(0.1)
            failed_pages = await asyncio.wait_for(self._probe_pages(port, deadline), max(0.001, deadline - time.monotonic()))
            if failed_pages:
                state = "failed"
                reason = "；".join(f"{page['reason']}：{page['url']}" for page in failed_pages)
            else:
                state = "ok"
                reason = ""
        except (TimeoutError, websocket.WebSocketTimeoutException) as exc:
            state = "failed"
            reason = "CDP 健康检查超时"
            event('cdp.health_check.error', level=logging.WARNING, error_type=type(exc).__name__,
                  error=str(exc), exc_info=True)
        except Exception as exc:
            state = "failed"
            reason = f"CDP 健康检查失败：{type(exc).__name__}"
            event('cdp.health_check.error', level=logging.ERROR, error_type=type(exc).__name__,
                  error=str(exc), exc_info=True)
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
            return await asyncio.gather(*(self._check(profile_id, port, pid) for profile_id, port, pid in running))

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
