from pathlib import Path
from http.client import BadStatusLine
from unittest.mock import Mock

import psutil
import pytest
from fastapi.testclient import TestClient

from chrome_manager.config import Settings
from chrome_manager.core.chrome_service import ChromeService, ChromeStartError
from chrome_manager.core.process_manager import ProcessManager
from chrome_manager.core.profile_manager import ProfileManager, ProfileError
from chrome_manager.db.database import Database
from chrome_manager.web import create_app
from chrome_manager.web import ResourceMonitor


@pytest.fixture
def profiles(tmp_path):
    database = Database(tmp_path / 'test.db')
    database.initialize()
    return ProfileManager(database, tmp_path / 'profiles')


@pytest.mark.parametrize('name', ['..', '.', 'CON', 'nul.txt', 'COM1', 'trailing.'])
def test_windows_reserved_names_rejected(profiles, name):
    with pytest.raises(ProfileError):
        profiles.create(name, cdp_port=9511)


@pytest.mark.parametrize('proxy', ['http://localhost:abc', 'http://localhost:99999', 'http://[broken', 'http://localhost:0', 'http://localhost:7890/path'])
def test_malformed_proxy_is_validation_error(proxy):
    with pytest.raises(ProfileError):
        ProfileManager._parse_proxy(proxy)


def test_launch_failure_can_be_retried(profiles, tmp_path, monkeypatch):
    profile = profiles.create('retry', cdp_port=9511)
    service = ChromeService(profiles.database, Settings(tmp_path))
    monkeypatch.setattr(service, '_find_chrome', lambda: Path('chrome.exe'))
    monkeypatch.setattr('chrome_manager.core.chrome_service.is_port_available', lambda port: True)
    launch = Mock(side_effect=OSError('launch denied'))
    monkeypatch.setattr('chrome_manager.core.chrome_service.subprocess.Popen', launch)
    with pytest.raises(ChromeStartError):
        service.start(profile)
    assert profiles.show('retry').status == 'stopped'


def test_custom_web_port_and_direct_proxy(profiles, tmp_path, monkeypatch):
    profile = profiles.create('custom', cdp_port=9511, default_url='https://example.com, https://example.org')
    service = ChromeService(profiles.database, Settings(tmp_path), web_port=8877)
    monkeypatch.setattr(service, '_find_chrome', lambda: Path('chrome.exe'))
    monkeypatch.setattr('chrome_manager.core.chrome_service.is_port_available', lambda port: True)
    launch = Mock(side_effect=OSError('stop before launching'))
    monkeypatch.setattr('chrome_manager.core.chrome_service.subprocess.Popen', launch)
    with pytest.raises(ChromeStartError):
        service.start(profile)
    args = launch.call_args.args[0]
    assert '--no-proxy-server' in args
    assert args[-3:] == ['http://127.0.0.1:8877/profiles/custom', 'https://example.com', 'https://example.org']


def test_manual_exit_reconciled(profiles, monkeypatch):
    profile = profiles.create('closed', cdp_port=9511)
    with profiles.database.transaction() as db:
        db.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
        db.execute('INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port) VALUES (?,123456,1,9511)', (profile.id,))
    monkeypatch.setattr('chrome_manager.core.process_manager.psutil.Process', Mock(side_effect=psutil.NoSuchProcess(123456)))
    ProcessManager(profiles.database, 1).reconcile()
    assert profiles.show('closed').status == 'stopped'


def test_web_routes_and_cross_site_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    with TestClient(create_app()) as client:
        assert client.get('/profiles', follow_redirects=False).status_code == 303
        assert client.get('/').status_code == 200
        assert client.get('/api/resources').json()['samples']
        assert client.post('/profiles', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/profiles/missing').status_code == 404


def test_edit_invalid_proxy_and_running_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    app = create_app()
    database = Database(tmp_path / 'data' / 'chrome_manager.db')
    manager = ProfileManager(database, tmp_path / 'profiles')
    profile = manager.create('edit', cdp_port=9511)
    with TestClient(app) as client:
        response = client.post('/profiles/edit/edit', data={'proxy_enabled': 'true', 'proxy_url': 'http://localhost:bad'}, follow_redirects=False)
        assert response.status_code == 303
        assert 'error=' in response.headers['location']
        with database.transaction() as db:
            db.execute("UPDATE profiles SET status='starting' WHERE id=?", (profile.id,))
        assert client.get('/profiles/edit/edit', follow_redirects=False).status_code == 303


def test_monitor_recovers_after_sample_error(profiles, monkeypatch):
    monitor = ResourceMonitor(profiles.database, profiles)
    real_sample = monitor.sample
    calls = 0

    def sample():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('temporary sampling failure')
        return real_sample()

    monkeypatch.setattr(monitor, 'sample', sample)
    monkeypatch.setattr(monitor.stop_event, 'wait', Mock(side_effect=[False, False, True]))
    monitor.start()
    monitor.thread.join(timeout=5)
    assert calls == 3
    assert monitor.error is None
    assert len(monitor.samples()) == 2


def test_stop_requests_graceful_close_first(profiles, monkeypatch):
    profile = profiles.create('stop', cdp_port=9511)
    with profiles.database.transaction() as db:
        db.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
        db.execute('INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port) VALUES (?,123456,1,9511)', (profile.id,))
    process = Mock()
    process.create_time.return_value = 1
    process.cmdline.return_value = [f'--user-data-dir={profile.user_data_dir}', '--remote-debugging-port=9511']
    monkeypatch.setattr('chrome_manager.core.process_manager.psutil.Process', Mock(return_value=process))
    manager = ProcessManager(profiles.database, 1)
    close = Mock()
    monkeypatch.setattr(manager, '_request_close', close)
    manager.stop_profile(profile)
    close.assert_called_once_with(process.pid)
    process.kill.assert_not_called()
    assert profiles.show('stop').status == 'stopped'


def test_cdp_retries_malformed_http_response(monkeypatch):
    opener = Mock()
    opener.open.side_effect = BadStatusLine('malformed response')
    monkeypatch.setattr('chrome_manager.core.chrome_service.build_opener', Mock(return_value=opener))
    monkeypatch.setattr('chrome_manager.core.chrome_service.time.monotonic', Mock(side_effect=[0, 0, 2]))
    monkeypatch.setattr('chrome_manager.core.chrome_service.time.sleep', Mock())
    with pytest.raises(ChromeStartError):
        ChromeService._wait_for_cdp(19522, 1)
    opener.open.assert_called_once()
