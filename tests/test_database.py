import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from chrome_manager.db.database import Database
from chrome_manager.exceptions import DatabaseError


def test_initialization_creates_current_schema_and_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    with closing(database.connect()) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"profiles", "cdp_ports", "proxies", "runtime_sessions", "events", "settings", "schema_version"} <= tables
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 8
        assert not {'cdp_alert_incidents', 'notification_outbox', 'resource_samples'} & tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_existing_v3_database_upgrades_without_losing_profiles(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("UPDATE schema_version SET version = 3")
        connection.execute("INSERT INTO profiles(name, user_data_dir) VALUES ('existing', 'existing-data')")
    database.initialize()
    with database.read() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 8
        assert connection.execute("SELECT name FROM profiles").fetchone()[0] == "existing"


def test_migration_rebases_legacy_managed_profile_paths(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("INSERT INTO profiles(name, user_data_dir) VALUES (?, ?)",
                           ("account", r"D:\OldLocation\.data\profiles\account\User Data"))
        connection.execute("INSERT INTO profiles(name, user_data_dir) VALUES (?, ?)",
                           ("renamed", r"D:\OldLocation\.data\profiles\original-folder\User Data"))
        connection.execute("UPDATE schema_version SET version = 4")
    database.initialize()
    with database.read() as connection:
        paths = {row[0]: row[1] for row in connection.execute("SELECT name, user_data_dir FROM profiles")}
        assert paths == {"account": "profiles/account/User Data", "renamed": "profiles/original-folder/User Data"}


@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_wal_negotiation_recovers_from_a_transient_startup_lock(tmp_path, monkeypatch, code):
    connection = Mock()
    locked = sqlite3.OperationalError('database is locked')
    locked.sqlite_errorcode = code
    attempts = 0

    def execute(sql):
        nonlocal attempts
        if sql == 'PRAGMA journal_mode = WAL':
            attempts += 1
            if attempts == 1:
                raise locked
        return Mock()

    connection.execute.side_effect = execute
    monkeypatch.setattr(sqlite3, 'connect', Mock(return_value=connection))
    monkeypatch.setattr('chrome_manager.db.database.time.sleep', Mock())
    assert Database(tmp_path / 'database.db').connect() is connection
    assert attempts == 2
    connection.close.assert_not_called()


def test_wal_startup_lock_timeout_closes_the_connection(tmp_path, monkeypatch):
    connection = Mock()
    locked = sqlite3.OperationalError('database is locked')
    locked.sqlite_errorcode = sqlite3.SQLITE_BUSY
    connection.execute.side_effect = [None, None, locked]
    monkeypatch.setattr(sqlite3, 'connect', Mock(return_value=connection))
    monkeypatch.setattr('chrome_manager.db.database.time.monotonic', Mock(side_effect=[0, 6]))
    with pytest.raises(DatabaseError) as caught:
        Database(tmp_path / 'database.db').connect()
    assert caught.value.__cause__ is locked
    connection.close.assert_called_once()


def test_wal_non_lock_errors_are_not_retried(tmp_path, monkeypatch):
    connection = Mock()
    broken = sqlite3.OperationalError('disk I/O error')
    broken.sqlite_errorcode = sqlite3.SQLITE_IOERR
    connection.execute.side_effect = [None, None, broken]
    sleeper = Mock()
    monkeypatch.setattr(sqlite3, 'connect', Mock(return_value=connection))
    monkeypatch.setattr('chrome_manager.db.database.time.sleep', sleeper)
    with pytest.raises(DatabaseError) as caught:
        Database(tmp_path / 'database.db').connect()
    assert caught.value.__cause__ is broken
    sleeper.assert_not_called()
    connection.close.assert_called_once()
