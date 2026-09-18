"""Profile metadata and independent user-data directory management."""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlsplit
from pathlib import Path

from chrome_manager.core.chrome_service import is_port_available
from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile, ProfileRepository

_INVALID_PROFILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ProfileError(ValueError):
    """Raised when a profile request conflicts with the V1 rules."""


class ProfileManager:
    def __init__(self, database: Database, profiles_root: Path) -> None:
        self.database = database
        self.profiles_root = profiles_root
        self.repository = ProfileRepository()

    def create(
        self,
        name: str,
        *,
        project_name: str | None = None,
        platform: str | None = None,
        account_name: str | None = None,
        description: str | None = None,
        tags: str | None = None,
        cdp_port: int | None = None,
        default_url: str | None = None,
        proxy_url: str | None = None,
    ) -> Profile:
        self._validate_name(name)
        if cdp_port is not None and not 9500 <= cdp_port <= 65535:
            raise ProfileError("CDP 端口必须在 9500 到 65535 之间")
        user_data_dir = self.profiles_root / name / "User Data"
        normalized_url = self._validate_default_url(default_url)
        proxy = self._parse_proxy(proxy_url)
        try:
            with self.database.transaction() as connection:
                existing = self.repository.get_any_by_name(connection, name)
                if existing is not None and not existing.is_deleted:
                    raise ProfileError(f"Profile「{name}」已存在")
                if cdp_port is not None and connection.execute(
                    "SELECT 1 FROM cdp_ports WHERE port = ?", (cdp_port,)
                ).fetchone():
                    raise ProfileError(f"CDP 端口 {cdp_port} 已被占用")
                assigned_port = cdp_port or self._find_auto_port(connection)
                if existing is not None:
                    user_data_dir.mkdir(parents=True, exist_ok=True)
                    profile = self.repository.revive(
                        connection, profile=existing, project_name=project_name, platform=platform,
                        account_name=account_name, description=description, tags=tags, cdp_port=assigned_port,
                        default_url=normalized_url, proxy=proxy,
                    )
                else:
                    user_data_dir.mkdir(parents=True, exist_ok=False)
                    profile = self.repository.create(
                        connection, name=name, user_data_dir=str(user_data_dir), project_name=project_name,
                        platform=platform, account_name=account_name, description=description, tags=tags,
                        cdp_port=assigned_port, default_url=normalized_url, proxy=proxy,
                    )
                connection.execute(
                    "INSERT INTO events(profile_id, event_type, message) VALUES (?, ?, ?)",
                    (profile.id, "profile_restored" if existing else "profile_created", "Profile restored" if existing else "Profile created"),
                )
                return profile
        except FileExistsError as exc:
            raise ProfileError(f"Profile「{name}」的数据目录已存在，无法安全创建") from exc
        except sqlite3.IntegrityError as exc:
            raise ProfileError("Profile 名称、数据目录或 CDP 端口已存在") from exc

    def list(self) -> list[Profile]:
        with self.database.read() as connection:
            return self.repository.list_active(connection)

    def suggested_port(self) -> int:
        """Use the next port after the current highest managed CDP port."""
        with self.database.read() as connection:
            row = connection.execute("SELECT MAX(port) AS port FROM cdp_ports WHERE port >= 9500").fetchone()
            start = max(9500, (row["port"] or 9499) + 1)
            assigned = {item[0] for item in connection.execute("SELECT port FROM cdp_ports")}
        for port in range(start, 65536):
            if port not in assigned and is_port_available(port):
                return port
        raise ProfileError("没有可用的 9500 以上 CDP 端口")

    def show(self, name: str) -> Profile:
        with self.database.read() as connection:
            profile = self.repository.get_by_name(connection, name)
        if profile is None:
            raise ProfileError(f"未找到 Profile「{name}」")
        return profile

    def update(
        self, name: str, *, project_name: str | None, platform: str | None, account_name: str | None,
        description: str | None, default_url: str | None, proxy_url: str | None,
    ) -> Profile:
        normalized_url = self._validate_default_url(default_url)
        proxy = self._parse_proxy(proxy_url)
        with self.database.transaction() as connection:
            profile = self.repository.get_by_name(connection, name)
            if profile is None:
                raise ProfileError(f"未找到 Profile「{name}」")
            if profile.status != "stopped":
                raise ProfileError("Profile 正在运行中，请先停止后再编辑")
            updated = self.repository.update_metadata(
                connection, profile=profile, project_name=project_name, platform=platform,
                account_name=account_name, description=description, default_url=normalized_url, proxy=proxy,
            )
            connection.execute(
                "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'profile_updated', ?)",
                (profile.id, "Profile metadata updated"),
            )
            return updated

    def delete(self, name: str) -> None:
        with self.database.transaction() as connection:
            profile = self.repository.get_by_name(connection, name)
            if profile is None:
                raise ProfileError(f"未找到 Profile「{name}」")
            if profile.status != "stopped":
                raise ProfileError("请先停止 Profile，再执行删除")
            self.repository.soft_delete(connection, profile)
            connection.execute(
                "INSERT INTO events(profile_id, event_type, message) VALUES (?, 'profile_deleted', ?)",
                (profile.id, "Profile soft-deleted"),
            )

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or name.strip() != name:
            raise ProfileError("Profile 名称不能为空，且不能包含首尾空格")
        if len(name) > 100 or _INVALID_PROFILE_NAME.search(name):
            raise ProfileError("Profile 名称包含 Windows 路径非法字符")

    @staticmethod
    def _validate_default_url(default_url: str | None) -> str | None:
        if not default_url:
            return None
        values = [value.strip() for value in default_url.replace("，", ",").split(",") if value.strip()]
        if not values:
            return None
        for value in values:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ProfileError("每个默认打开网址都必须是完整的 http 或 https 地址，多个网址请用逗号分隔")
        return ", ".join(values)

    @staticmethod
    def _parse_proxy(proxy_url: str | None) -> tuple[str, str, int] | None:
        if not proxy_url:
            return None
        parsed = urlsplit(proxy_url.strip())
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or parsed.port is None:
            raise ProfileError("代理地址必须为 http、https 或 socks5 格式，例如 http://127.0.0.1:7890")
        if parsed.username or parsed.password:
            raise ProfileError("当前版本不支持带用户名或密码的代理地址")
        return parsed.scheme, parsed.hostname, parsed.port

    @staticmethod
    def _find_auto_port(connection: sqlite3.Connection) -> int:
        assigned = {row[0] for row in connection.execute("SELECT port FROM cdp_ports")}
        highest = connection.execute("SELECT MAX(port) FROM cdp_ports WHERE port >= 9500").fetchone()[0]
        for port in range(max(9500, (highest or 9499) + 1), 65536):
            if port not in assigned and is_port_available(port):
                return port
        raise ProfileError("没有可用的 9500 以上 CDP 端口")
