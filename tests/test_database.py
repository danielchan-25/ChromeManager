import sqlite3
from pathlib import Path

from chrome_manager.db.database import Database


def test_initialization_creates_current_schema_and_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    with database.connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"profiles", "cdp_ports", "proxies", "runtime_sessions", "events", "settings", "schema_version"} <= tables
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
