import io
import json
import socket
import subprocess
import sys
import time
from unittest.mock import AsyncMock, Mock

import pytest
import psutil
from fastapi.testclient import TestClient

from chrome_manager.core import cdp
from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.chrome_service import is_port_available
from chrome_manager.core.service_lock import service_lock, ServiceAlreadyRunning
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.web import create_app


def test_bound_port_cannot_be_reused():
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        assert not is_port_available(occupied.getsockname()[1])


@pytest.mark.parametrize('url', ['ws://example.com:9600/page', 'ws://127.0.0.1:9601/page',
                                     'ws://user:pass@127.0.0.1:9600/page', 'https://127.0.0.1:9600/page'])
def test_probe_rejects_redirect_to_another_browser(url, monkeypatch):
    connect = Mock()
    monkeypatch.setattr(cdp.websocket, 'create_connection', connect)
    with pytest.raises(ValueError):
        cdp.command(url, 9600, 'Browser.getVersion', timeout=1)
    connect.assert_not_called()


def test_browser_probe_rejects_wrong_pid(monkeypatch):
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=io.StringIO(
        json.dumps({'webSocketDebuggerUrl': 'ws://127.0.0.1:9600/devtools/browser/test'})))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cdp, 'build_opener', Mock(return_value=opener))
    monkeypatch.setattr(cdp, 'command', Mock(side_effect=[
        {'product': 'Chrome/test', 'protocolVersion': '1.3'},
        {'processInfo': [{'type': 'browser', 'id': 999}]},
    ]))
    with pytest.raises(ValueError, match='different browser'):
        cdp.probe_browser(9600, 1, 123)


def test_browser_probe_verifies_pid_without_scanning_system_sockets(monkeypatch):
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=io.StringIO(
        json.dumps({'webSocketDebuggerUrl': 'ws://127.0.0.1:9600/devtools/browser/test'})))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cdp, 'build_opener', Mock(return_value=opener))
    commands = Mock(side_effect=[
        {'product': 'Chrome/test', 'protocolVersion': '1.3'},
        {'processInfo': [{'type': 'browser', 'id': 123}]},
    ])
    monkeypatch.setattr(cdp, 'command', commands)
    scan = Mock(side_effect=AssertionError('Socket enumeration must not consume the probe deadline'))
    monkeypatch.setattr(psutil, 'Process', scan)
    assert cdp.probe_browser(9600, 1, 123)['product'] == 'Chrome/test'
    assert commands.call_args.args[2] == 'SystemInfo.getProcessInfo'
    scan.assert_not_called()


def test_cdp_errors_close_socket(monkeypatch):
    connection = Mock()
    connection.recv.return_value = '{"id":1,"error":{"message":"failed"}}'
    monkeypatch.setattr(cdp.websocket, 'create_connection', Mock(return_value=connection))
    with pytest.raises(ValueError):
        cdp.command('ws://127.0.0.1:9600/page', 9600, 'Browser.getVersion', timeout=1)
    connection.close.assert_called_once_with(timeout=0)


def test_failed_instance_does_not_fail_other_instances(tmp_path, monkeypatch):
    monitor = CdpMonitor(Database(tmp_path / 'test.db'))
    monkeypatch.setattr(monitor, '_running', lambda: [(1, 9600, 111), (2, 9601, 222)])
    monkeypatch.setattr(monitor, '_probe_pages', AsyncMock(return_value=[]))

    def probe(port, timeout, pid):
        if port == 9600:
            raise TimeoutError()

    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', probe)
    result = monitor.check_once()
    assert result[1]['state'] == 'failed'
    assert result[2]['state'] == 'ok'


def test_stale_check_is_not_ready(tmp_path):
    monitor = CdpMonitor(Database(tmp_path / 'test.db'), interval=1, timeout_ms=100)
    monitor._states = {1: {'state': 'ok', 'checked_at': time.time() - 10}}
    assert monitor.states()[1]['state'] == 'failed'
    assert monitor._states[1]['state'] == 'ok'


def test_lock_rejects_parallel_start_and_recovers_after_crash(tmp_path):
    code = ('from pathlib import Path; from chrome_manager.core.service_lock import service_lock; '
            'import sys,time,os; lock=service_lock(Path(sys.argv[1])); lock.__enter__(); '
            'print("locked",os.getpid(),flush=True); time.sleep(60)')
    process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline().strip().split()
        assert line[0] == 'locked'
        actual_pid = int(line[1])
        with pytest.raises(ServiceAlreadyRunning):
            with service_lock(tmp_path):
                pass
    finally:
        # A Windows venv may add a launcher parent. Kill the interpreter that
        # actually owns the lock rather than leaving it alive behind the launcher.
        if 'actual_pid' in locals() and actual_pid != process.pid:
            child = psutil.Process(actual_pid)
            child.kill()
            child.wait(timeout=5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
    with service_lock(tmp_path):
        pass


def test_per_port_health_rejects_stopped_checking_and_wrong_session(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    app = create_app()
    database = Database(tmp_path / 'data/chrome_manager.db')
    profile = ProfileManager(database, tmp_path / 'profiles').create('api', cdp_port=9600)
    # No lifespan: keep background reconciliation from changing the synthetic session.
    client = TestClient(app)
    assert client.get('/api/cdp/9601/health').status_code == 404
    assert client.get('/api/cdp/9600/health').json()['state'] == 'stopped'
    with database.transaction() as db:
        db.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
        db.execute('INSERT INTO runtime_sessions(profile_id,pid,cdp_port) VALUES (?,123,9600)', (profile.id,))
    assert client.get('/api/cdp/9600/health').status_code == 503
    state = {'pid': 456, 'state': 'ok', 'checked_at': int(time.time())}
    monkeypatch.setattr(CdpMonitor, 'states', lambda self: {profile.id: state})
    assert client.get('/api/cdp/9600/health').json()['ready'] is False
    state['pid'] = 123
    response = client.get('/api/cdp/9600/health')
    assert response.status_code == 200 and response.json()['ready'] is True
    assert response.headers['cache-control'] == 'no-store'
    state['state'] = 'failed'
    assert client.get('/api/cdp/9600/health').status_code == 503
