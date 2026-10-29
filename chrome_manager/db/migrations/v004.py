"""Reserved schema revision for compatibility with older installations."""

from __future__ import annotations

import sqlite3

VERSION = 4


def apply(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE schema_version SET version = ?", (VERSION,))
