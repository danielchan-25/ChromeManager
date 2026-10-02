"""Store managed Chrome profile directories relative to the data root."""

from __future__ import annotations

import sqlite3
from pathlib import PureWindowsPath

VERSION = 5


def apply(connection: sqlite3.Connection) -> None:
    for profile_id, stored in connection.execute("SELECT id, user_data_dir FROM profiles"):
        path = PureWindowsPath(stored)
        if (path.is_absolute() and len(path.parts) >= 4 and path.parts[-3].lower() == "profiles"
                and path.parts[-1].lower() == "user data"):
            connection.execute(
                "UPDATE profiles SET user_data_dir = ? WHERE id = ?",
                (f"profiles/{path.parts[-2]}/User Data", profile_id),
            )
    connection.execute("UPDATE schema_version SET version = ?", (VERSION,))
