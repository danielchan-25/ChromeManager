"""SQLite connection and migration boundary."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from chrome_manager.exceptions import DatabaseError


class Database:
    """Owns SQLite setup; business modules should use repositories, not raw SQL."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except sqlite3.Error as exc:
            raise DatabaseError(f"Unable to open database: {self.path}") from exc

    def initialize(self) -> None:
        from chrome_manager.db.migrations import v001, v002, v003

        connection = self.connect()
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not exists:
                v001.apply(connection)
            version = connection.execute("SELECT version FROM schema_version").fetchone()
            if version is None:
                raise DatabaseError("Unsupported database schema version")
            if version["version"] == v001.VERSION:
                v002.apply(connection)
                version = connection.execute("SELECT version FROM schema_version").fetchone()
            if version is not None and version["version"] == v002.VERSION:
                v003.apply(connection)
                version = connection.execute("SELECT version FROM schema_version").fetchone()
            if version is None or version["version"] != v003.VERSION:
                raise DatabaseError("Unsupported database schema version")
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise DatabaseError("Database transaction failed") from exc
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Provide a short-lived read connection that is always released."""
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()
