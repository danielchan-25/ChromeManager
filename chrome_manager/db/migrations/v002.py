"""Release stale CDP allocations left by soft-deleted profiles."""

from __future__ import annotations

import sqlite3

VERSION = 2


def apply(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE profiles SET cdp_port_id = NULL WHERE is_deleted = 1")
    connection.execute("DELETE FROM cdp_ports WHERE profile_id IS NULL AND reserved = 0")
    connection.execute("UPDATE schema_version SET version = ?", (VERSION,))
