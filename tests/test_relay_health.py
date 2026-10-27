import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import psutil

from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.web import create_app


def test_monitor_checks_private_port_but_returns_public_port(tmp_path, monkeypatch):
    db = Database(tmp_path / 'data/chrome_manager.db'); db.initialize()
    p = ProfileManager(db, tmp_path / 'profiles').create('private', cdp_port=9600)
    with db.transaction() as c:
        c.execute("UPDATE profiles SET status='running' WHERE id=?", (p.id,))
        c.execute('INSERT INTO runtime_sessions(profile_id,pid,cdp_port) VALUES (?,123,20000)', (p.id,))
    monitor = CdpMonitor(db)
    probe = Mock(return_value={})
    monkeypatch.setattr('chrome_manager.core.cdp_monitor.probe_browser', probe)
    monkeypatch.setattr(monitor, '_probe_pages', AsyncMock(return_value=[]))
    states = monitor.check_once()
    probe.assert_called_once_with(20000, 2, 123)
    assert states[p.id]['port'] == 9600 and states[p.id]['state'] == 'ok'


def test_one_relay_failure_does_not_make_another_port_unready(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(CdpMonitor, 'start', lambda _: None)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_text('[relay]\nenabled=true\n', encoding='utf-8')
    app = create_app()
    db = Database(tmp_path / 'data/chrome_manager.db')
    manager = ProfileManager(db, tmp_path / 'profiles')
    profiles = [manager.create('bad', cdp_port=9600), manager.create('good', cdp_port=9601)]
    with db.transaction() as c:
        for p in profiles:
            c.execute("UPDATE profiles SET status='running' WHERE id=?", (p.id,))
            c.execute('INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port) VALUES (?,?,?,?)',
                      (p.id, os.getpid(), psutil.Process().create_time(), p.cdp_port))
    monkeypatch.setattr(CdpMonitor, 'states', lambda _: {p.id: {'pid': os.getpid(), 'state': 'ok', 'port': p.cdp_port, 'checked_at': int(time.time())} for p in profiles})
    status = {'checked_at': int(time.time()), 'error': None, 'errors': {'9600': 'occupied'},
              'listeners': {'9600': False, '9601': True}}
    (tmp_path / 'data/cdp-lan-status.json').write_text(json.dumps(status), encoding='utf-8')
    with TestClient(app) as client:
        assert client.get('/api/cdp/9600/health').json()['ready'] is False
        assert client.get('/api/cdp/9601/health').json()['ready'] is True
        status['checked_at'] -= 60
        (tmp_path / 'data/cdp-lan-status.json').write_text(json.dumps(status), encoding='utf-8')
        assert client.get('/api/cdp/9601/health').json()['ready'] is False
