"""Repository implementations; application code does not issue SQL directly."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Profile:
    id: int
    name: str
    project_name: str | None
    platform: str | None
    account_name: str | None
    description: str | None
    tags: str | None
    user_data_dir: str
    chrome_path: str | None
    status: str
    cdp_port: int | None
    is_deleted: bool
    default_url: str | None
    proxy_url: str | None


class ProfileRepository:
    def list_active(self, connection: sqlite3.Connection) -> list[Profile]:
        rows = connection.execute(
            """
            SELECT p.*, cp.port AS cdp_port,
                   CASE WHEN px.id IS NULL THEN NULL ELSE px.type || '://' || px.host || ':' || px.port END AS proxy_url
            FROM profiles p LEFT JOIN cdp_ports cp ON cp.profile_id = p.id
            LEFT JOIN proxies px ON px.id = p.proxy_id
            WHERE p.is_deleted = 0 ORDER BY cp.port IS NULL, cp.port, p.name COLLATE NOCASE
            """
        ).fetchall()
        return [self._to_profile(row) for row in rows]

    def get_by_name(self, connection: sqlite3.Connection, name: str) -> Profile | None:
        row = connection.execute(
            """
            SELECT p.*, cp.port AS cdp_port,
                   CASE WHEN px.id IS NULL THEN NULL ELSE px.type || '://' || px.host || ':' || px.port END AS proxy_url
            FROM profiles p LEFT JOIN cdp_ports cp ON cp.profile_id = p.id
            LEFT JOIN proxies px ON px.id = p.proxy_id
            WHERE p.name = ? AND p.is_deleted = 0
            """,
            (name,),
        ).fetchone()
        return self._to_profile(row) if row else None

    def get_any_by_name(self, connection: sqlite3.Connection, name: str) -> Profile | None:
        row = connection.execute(
            """
            SELECT p.*, cp.port AS cdp_port,
                   CASE WHEN px.id IS NULL THEN NULL ELSE px.type || '://' || px.host || ':' || px.port END AS proxy_url
            FROM profiles p LEFT JOIN cdp_ports cp ON cp.profile_id = p.id
            LEFT JOIN proxies px ON px.id = p.proxy_id
            WHERE p.name = ?
            """,
            (name,),
        ).fetchone()
        return self._to_profile(row) if row else None

    def create(
        self,
        connection: sqlite3.Connection,
        *,
        name: str,
        user_data_dir: str,
        project_name: str | None,
        platform: str | None,
        account_name: str | None,
        description: str | None,
        tags: str | None,
        cdp_port: int | None,
        default_url: str | None,
        proxy: tuple[str, str, int] | None,
    ) -> Profile:
        cursor = connection.execute(
            """
            INSERT INTO profiles(name, project_name, platform, account_name, description, tags, user_data_dir, default_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, project_name, platform, account_name, description, tags, user_data_dir, default_url),
        )
        profile_id = int(cursor.lastrowid)
        if cdp_port is not None:
            connection.execute(
                "INSERT INTO cdp_ports(port, purpose, profile_id, project_name, platform, account_name) VALUES (?, 'profile', ?, ?, ?, ?)",
                (cdp_port, profile_id, project_name, platform, account_name),
            )
            connection.execute(
                "UPDATE profiles SET cdp_port_id = (SELECT id FROM cdp_ports WHERE port = ?) WHERE id = ?",
                (cdp_port, profile_id),
            )
        self._set_proxy(connection, profile_id, proxy)
        profile = self.get_by_name(connection, name)
        assert profile is not None
        return profile

    def soft_delete(self, connection: sqlite3.Connection, profile: Profile) -> None:
        connection.execute("DELETE FROM cdp_ports WHERE profile_id = ? AND reserved = 0", (profile.id,))
        connection.execute(
            "UPDATE profiles SET is_deleted = 1, cdp_port_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (profile.id,),
        )

    def revive(
        self, connection: sqlite3.Connection, *, profile: Profile, project_name: str | None,
        platform: str | None, account_name: str | None, description: str | None, tags: str | None, cdp_port: int,
        default_url: str | None, proxy: tuple[str, str, int] | None,
    ) -> Profile:
        connection.execute("DELETE FROM proxies WHERE id = (SELECT proxy_id FROM profiles WHERE id = ?)", (profile.id,))
        connection.execute(
            "UPDATE profiles SET project_name = ?, platform = ?, account_name = ?, description = ?, tags = ?, default_url = ?, "
            "cdp_port_id = NULL, proxy_id = NULL, status = 'stopped', is_deleted = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_name, platform, account_name, description, tags, default_url, profile.id),
        )
        connection.execute(
            "INSERT INTO cdp_ports(port, purpose, profile_id, project_name, platform, account_name) VALUES (?, 'profile', ?, ?, ?, ?)",
            (cdp_port, profile.id, project_name, platform, account_name),
        )
        self._set_proxy(connection, profile.id, proxy)
        connection.execute(
            "UPDATE profiles SET cdp_port_id = (SELECT id FROM cdp_ports WHERE port = ?) WHERE id = ?",
            (cdp_port, profile.id),
        )
        revived = self.get_by_name(connection, profile.name)
        assert revived is not None
        return revived

    def update_metadata(
        self, connection: sqlite3.Connection, *, profile: Profile, project_name: str | None,
        platform: str | None, account_name: str | None, description: str | None,
        default_url: str | None, proxy: tuple[str, str, int] | None,
    ) -> Profile:
        connection.execute("DELETE FROM proxies WHERE id = (SELECT proxy_id FROM profiles WHERE id = ?)", (profile.id,))
        connection.execute(
            "UPDATE profiles SET project_name = ?, platform = ?, account_name = ?, description = ?, default_url = ?, "
            "proxy_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_name, platform, account_name, description, default_url, profile.id),
        )
        self._set_proxy(connection, profile.id, proxy)
        updated = self.get_by_name(connection, profile.name)
        assert updated is not None
        return updated

    @staticmethod
    def _set_proxy(connection: sqlite3.Connection, profile_id: int, proxy: tuple[str, str, int] | None) -> None:
        if proxy is None:
            return
        proxy_type, host, port = proxy
        cursor = connection.execute(
            "INSERT INTO proxies(name, type, host, port, enabled) VALUES (?, ?, ?, ?, 1)",
            (f"profile-{profile_id}", proxy_type, host, port),
        )
        connection.execute("UPDATE profiles SET proxy_id = ? WHERE id = ?", (cursor.lastrowid, profile_id))

    @staticmethod
    def _to_profile(row: sqlite3.Row) -> Profile:
        return Profile(
            id=row["id"], name=row["name"], project_name=row["project_name"], platform=row["platform"],
            account_name=row["account_name"], description=row["description"], tags=row["tags"],
            user_data_dir=row["user_data_dir"], chrome_path=row["chrome_path"], status=row["status"],
            cdp_port=row["cdp_port"], is_deleted=bool(row["is_deleted"]), default_url=row["default_url"],
            proxy_url=row["proxy_url"],
        )
