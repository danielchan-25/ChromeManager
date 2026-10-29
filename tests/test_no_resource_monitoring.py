"""Retired metrics cannot be collected, displayed or recreated after upgrading."""

import sqlite3
from unittest.mock import Mock

from fastapi.testclient import TestClient
import psutil
import pytest

from chrome_manager.core.instance_status import StatusSynchronizer
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.exceptions import DatabaseError
from chrome_manager.web import create_app


def legacy_database(tmp_path):
    database = Database(tmp_path / 'data/chrome_manager.db')
    database.initialize()
    profile = ProfileManager(database, tmp_path / 'profiles').create('preserved', cdp_port=9519)
    with database.transaction() as connection:
        connection.execute('CREATE TABLE resource_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, sampled_at INTEGER NOT NULL, payload TEXT NOT NULL)')
        connection.execute('CREATE INDEX resource_samples_time ON resource_samples(sampled_at)')
        connection.execute('INSERT INTO resource_samples(sampled_at,payload) VALUES (1,?)', ('{"cpu":99,"memory":2048}',))
        connection.execute('UPDATE schema_version SET version=7')
    return database, profile


def test_upgrade_removes_historical_metrics_and_index_preserving_instances(tmp_path):
    database, profile = legacy_database(tmp_path)
    database.initialize()
    database.initialize()
    with database.read() as connection:
        names = {row[0] for row in connection.execute('SELECT name FROM sqlite_master')}
        assert 'resource_samples' not in names
        assert 'resource_samples_time' not in names
        assert connection.execute("SELECT COUNT(*) FROM sqlite_sequence WHERE name='resource_samples'").fetchone()[0] == 0
        assert connection.execute('SELECT version FROM schema_version').fetchone()[0] == 8
        assert connection.execute('SELECT name FROM profiles WHERE id=?', (profile.id,)).fetchone()[0] == 'preserved'
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_failed_retirement_migration_rolls_back_cleanly(tmp_path, monkeypatch):
    from chrome_manager.db.migrations import v008
    database, _ = legacy_database(tmp_path)
    apply = v008.apply

    def fail(connection):
        apply(connection)
        raise sqlite3.OperationalError('interrupted deletion')

    monkeypatch.setattr(v008, 'apply', fail)
    with pytest.raises(DatabaseError):
        database.initialize()
    with database.read() as connection:
        assert connection.execute('SELECT version FROM schema_version').fetchone()[0] == 7
        assert connection.execute('SELECT COUNT(*) FROM resource_samples').fetchone()[0] == 1
    monkeypatch.setattr(v008, 'apply', apply)
    database.initialize()
    with database.read() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='resource_samples'").fetchone()


def test_status_and_health_never_collect_or_publish_resource_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    forbidden = Mock(side_effect=AssertionError('Retired metric collector was called'))
    monkeypatch.setattr(psutil, 'cpu_percent', forbidden)
    monkeypatch.setattr(psutil, 'virtual_memory', forbidden)
    monkeypatch.setattr(psutil.Process, 'memory_info', forbidden)
    with TestClient(create_app()) as client:
        database = Database(tmp_path / 'data/chrome_manager.db')
        profile = ProfileManager(database, tmp_path / 'profiles').create('status-only', cdp_port=9519)
        assert client.get('/api/resources').status_code == 404
        payload = client.get('/api/status').json()
        assert payload['instances'][str(profile.id)]['status'] == 'stopped'
        assert payload['checked_at'] > 0
        assert not {'samples', 'system', 'alerts', 'cpu', 'memory'} & payload.keys()
        assert client.get('/health').json()['status'] == 'ok'
        page = client.get('/').text
        assert '实例资源走势' not in page
        assert 'CPU' not in page and '内存' not in page
        assert '<canvas' not in page
        assert 'desktop-resource-data' not in page
    forbidden.assert_not_called()


def test_state_synchronizer_still_updates_externally_closed_instance(tmp_path, monkeypatch):
    database = Database(tmp_path / 'database.sqlite3')
    database.initialize()
    profiles = ProfileManager(database, tmp_path / 'profiles')
    profile = profiles.create('closed', cdp_port=9519)
    with database.transaction() as connection:
        connection.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
        connection.execute('INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port) VALUES (?,?,?,?)',
                           (profile.id, 99999999, 1, 9519))
    sync = StatusSynchronizer(database, profiles)
    sync.refresh()
    assert profiles.show(profile.name).status == 'stopped'
    assert sync.snapshot()['checked_at'] > 0
    assert sync.snapshot()['warning'] is None
