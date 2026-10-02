"""Persist bounded resource samples across management-service restarts."""

from __future__ import annotations

import sqlite3

VERSION = 4


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE resource_samples ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sampled_at INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute("CREATE INDEX resource_samples_time ON resource_samples(sampled_at)")
    connection.execute("UPDATE schema_version SET version = ?", (VERSION,))
