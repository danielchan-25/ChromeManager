"""Add a per-profile startup URL."""

from __future__ import annotations

import sqlite3

VERSION = 3


def apply(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE profiles ADD COLUMN default_url TEXT")
    connection.execute("UPDATE schema_version SET version = ?", (VERSION,))
