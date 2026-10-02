import sqlite3
from pathlib import Path

from chrome_manager.db.database import Database


def test_initialization_creates_current_schema_and_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    with database.connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"profiles", "cdp_ports", "proxies", "runtime_sessions", "events", "settings", "schema_version", "resource_samples"} <= tables
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_existing_v3_database_upgrades_without_losing_profiles(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("DROP TABLE resource_samples")
        connection.execute("UPDATE schema_version SET version = 3")
        connection.execute("INSERT INTO profiles(name, user_data_dir) VALUES ('existing', 'existing-data')")
    database.initialize()
    with database.read() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5
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
