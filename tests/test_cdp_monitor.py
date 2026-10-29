import asyncio
import os
import time
from unittest.mock import AsyncMock, Mock

import psutil
import websocket
from fastapi.testclient import TestClient

from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.web import create_app


def test_probe_reports_browser_timeout(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / "test.db"), timeout_ms=250)
    monkeypatch.setattr(monitor, "_probe_pages", AsyncMock(return_value=[]))
    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', Mock(side_effect=TimeoutError()))
    profile_id, result = asyncio.run(monitor._check(7, 9510, 1234))
    assert profile_id == 7
    assert result["state"] == "failed"
    assert result["reason"] == "CDP 健康检查超时"


def test_stuck_page_is_identified_without_playwright_attachment(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / "test.db"))
    stuck = {"id": "docs", "url": "http://127.0.0.1:8000/docs", "reason": "页面 CDP 响应超时"}
    monkeypatch.setattr(monitor, "_probe_pages", AsyncMock(return_value=[stuck]))
    probe = Mock()
    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', probe)
    _, result = asyncio.run(monitor._check(5, 9510, 1234))
    assert result["state"] == "failed"
    assert result["failed_pages"] == [stuck]
    assert "http://127.0.0.1:8000/docs" in result["reason"]
    probe.assert_called_once_with(9510, 2, 1234)


def test_transient_browser_timeout_is_rechecked_before_reporting_failure(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / 'test.db'))
    pages = AsyncMock(return_value=[])
    monkeypatch.setattr(monitor, '_probe_pages', pages)
    probe = Mock(side_effect=[TimeoutError('PID response was delayed'), {}])
    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', probe)
    _, result = asyncio.run(monitor._check(4, 9600, 1234))
    assert result['state'] == 'ok'
    assert probe.call_count == 2
    assert probe.call_args_list[0].args == (9600, 2, 1234)
    assert probe.call_args_list[1].args == (9600, 4, 1234)
    pages.assert_awaited_once()


def test_persistent_timeout_remains_failed_after_bounded_retry(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / 'test.db'))
    pages = AsyncMock(return_value=[])
    monkeypatch.setattr(monitor, '_probe_pages', pages)
    probe = Mock(side_effect=TimeoutError('still unavailable'))
    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', probe)
    _, result = asyncio.run(monitor._check(4, 9600, 1234))
    assert result['state'] == 'failed'
    assert probe.call_count == 2
    pages.assert_not_awaited()


def test_pid_mismatch_is_not_hidden_by_timeout_retry(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / 'test.db'))
    pages = AsyncMock(return_value=[])
    monkeypatch.setattr(monitor, '_probe_pages', pages)
    probe = Mock(side_effect=ValueError('CDP port belongs to a different browser process'))
    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', probe)
    _, result = asyncio.run(monitor._check(4, 9600, 1234))
    assert result['state'] == 'failed'
    probe.assert_called_once()
    pages.assert_not_awaited()


def test_page_socket_without_command_responses_is_not_healthy(monkeypatch):
    connection = Mock()
    connection.recv.side_effect = websocket.WebSocketTimeoutException("timed out")
    monkeypatch.setattr(websocket, "create_connection", Mock(return_value=connection))
    result = CdpMonitor._probe_page({"id": "task", "url": "https://user:secret@host/path?token=private#fragment",
                                    "webSocketDebuggerUrl": "ws://127.0.0.1:9510/devtools/page/task"}, 0.25)
    assert result["reason"] == "页面 CDP 响应超时"
    assert result["url"] == "https://host/path"
    connection.close.assert_called_once()


def test_page_probe_waits_for_both_responses_and_ignores_events(monkeypatch):
    connection = Mock()
    connection.recv.side_effect = ['{"method":"Page.frameNavigated"}', '{"id":2,"result":{}}', '{"id":1,"result":{}}']
    monkeypatch.setattr(websocket, "create_connection", Mock(return_value=connection))
    assert CdpMonitor._probe_page({"id": "task", "url": "about:blank",
                                  "webSocketDebuggerUrl": "ws://127.0.0.1:9510/devtools/page/task"}, 0.25) is None
    assert connection.recv.call_count == 3
    connection.close.assert_called_once()


def test_tab_closed_by_a_task_during_probe_is_not_reported_as_stuck(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / "test.db"))
    target = {"id": "closed-task"}
    monkeypatch.setattr(monitor, "_targets", Mock(side_effect=[[target], []]))
    monkeypatch.setattr(monitor, "_probe_page", lambda _target, _timeout: {
        "id": "closed-task", "url": "https://example.com", "reason": "页面 CDP 命令失败"})
    assert asyncio.run(monitor._probe_pages(9510, time.monotonic() + 1)) == []


def test_health_and_page_show_unresponsive_cdp(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROME_MANAGER_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(CdpMonitor, "start", lambda self: None)
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    profile = ProfileManager(database, tmp_path / "profiles").create("stale", cdp_port=9510)
    pid = os.getpid()
    with database.transaction() as connection:
        connection.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
        connection.execute(
            "INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port,started_at) "
            "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
            (profile.id, pid, psutil.Process(pid).create_time(), 9510),
        )
    monkeypatch.setattr(CdpMonitor, "states", lambda self: {
        profile.id: {"state": "failed", "reason": "Playwright 连接超时", "pid": pid,
                     "port": 9510, "checked_at": int(time.time()), "latency_ms": 8000}
    })

    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["cdp_failed"] == [profile.id]
        assert client.get("/api/status").json()["cdp"][str(profile.id)]["state"] == "failed"
        assert "CDP 连接失败" in client.get("/").text
