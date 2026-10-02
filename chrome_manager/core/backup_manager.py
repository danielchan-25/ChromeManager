"""Manual, recoverable backups of stopped Chrome profile data."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import psutil

from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile

_BACKUP_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}\Z")


class BackupError(RuntimeError):
    """A backup or restore could not be completed without risking profile data."""


class BackupManager:
    def __init__(self, database: Database, profiles_root: Path, backups_root: Path) -> None:
        self.database = database
        self.profiles_root = profiles_root.resolve()
        self.backups_root = backups_root.resolve()

    def _source(self, profile: Profile) -> Path:
        source = Path(profile.user_data_dir)
        if (source.is_symlink() or source.name != "User Data"
                or source.resolve().parent.parent != self.profiles_root):
            raise BackupError("Profile 数据目录不在受管范围内")
        return source

    def _backup_root(self, profile: Profile) -> Path:
        return self.backups_root / str(profile.id)

    @staticmethod
    def _new_id() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]

    def list(self, profile: Profile) -> list[dict[str, str]]:
        root = self._backup_root(profile)
        if not root.is_dir():
            return []
        backups = []
        for item in root.iterdir():
            if (item.is_symlink() or not item.is_dir() or not _BACKUP_ID.fullmatch(item.name)
                    or (item / "data").is_symlink() or not (item / "data").is_dir()):
                continue
            try:
                manifest = json.loads((item / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if manifest.get("profile_id") == profile.id and manifest.get("profile_name") == profile.name:
                backups.append({"id": item.name, "created_at": manifest.get("created_at", item.name),
                                "kind": manifest.get("kind", "manual")})
        return sorted(backups, key=lambda backup: backup["id"], reverse=True)

    def _claim(self, profile: Profile) -> None:
        if profile.status != "stopped":
            raise BackupError("请先停止实例，再备份或恢复数据")
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE profiles SET status='maintenance' WHERE id=? AND status='stopped' AND is_deleted=0",
                (profile.id,),
            )
            if changed.rowcount != 1:
                raise BackupError("实例状态已改变，请刷新后重试")

    def _release(self, profile: Profile) -> None:
        with self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status='stopped' WHERE id=? AND status='maintenance'", (profile.id,))

    @staticmethod
    def _assert_not_running(source: Path) -> None:
        expected = f"--user-data-dir={source}"
        for process in psutil.process_iter(["cmdline"]):
            try:
                if expected in (process.info["cmdline"] or []):
                    raise BackupError("检测到 Chrome 仍在使用该数据目录，请先关闭浏览器")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    @staticmethod
    def _write_manifest(path: Path, profile: Profile, kind: str) -> None:
        path.mkdir(parents=True, exist_ok=False)
        (path / "manifest.json").write_text(json.dumps({
            "profile_id": profile.id, "profile_name": profile.name,
            "created_at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
        }, ensure_ascii=False), encoding="utf-8")

    def backup(self, profile: Profile) -> str:
        source = self._source(profile)
        self._claim(profile)
        stage: Path | None = None
        try:
            self._assert_not_running(source)
            if not source.is_dir():
                raise BackupError("Profile 数据目录不存在")
            root = self._backup_root(profile)
            root.mkdir(parents=True, exist_ok=True)
            backup_id = self._new_id()
            stage = root / f".{backup_id}.tmp"
            self._write_manifest(stage, profile, "manual")
            shutil.copytree(source, stage / "data", symlinks=True)
            self._assert_not_running(source)
            stage.rename(root / backup_id)
            logging.getLogger("chrome_manager.backup").info("实例 %s 已备份：%s", profile.id, backup_id)
            return backup_id
        except (OSError, shutil.Error) as exc:
            raise BackupError(f"备份失败，原数据未更改：{type(exc).__name__}") from exc
        finally:
            try:
                if stage and stage.exists():
                    shutil.rmtree(stage)
            finally:
                self._release(profile)

    def restore(self, profile: Profile, backup_id: str) -> str:
        if not _BACKUP_ID.fullmatch(backup_id) or backup_id not in {item["id"] for item in self.list(profile)}:
            raise BackupError("所选备份不存在或不属于此实例")
        source = self._source(profile)
        self._claim(profile)
        stage = self.profiles_root / f".restore-{profile.id}-{uuid4().hex[:8]}"
        previous: Path | None = None
        try:
            self._assert_not_running(source)
            if not source.is_dir():
                raise BackupError("当前 Profile 数据目录不存在")
            backup_data = self._backup_root(profile) / backup_id / "data"
            shutil.copytree(backup_data, stage, symlinks=True)
            self._assert_not_running(source)
            previous = self._backup_root(profile) / self._new_id()
            self._write_manifest(previous, profile, "pre-restore")
            source.rename(previous / "data")
            try:
                stage.rename(source)
            except OSError:
                (previous / "data").rename(source)
                raise
            logging.getLogger("chrome_manager.backup").info(
                "实例 %s 已恢复备份 %s；恢复前数据保留在 %s", profile.id, backup_id, previous.name
            )
            return previous.name
        except (OSError, shutil.Error) as exc:
            raise BackupError(f"恢复失败，原数据已保留：{type(exc).__name__}") from exc
        finally:
            try:
                if stage.exists():
                    shutil.rmtree(stage)
            finally:
                if source.exists():
                    self._release(profile)
                else:
                    logging.getLogger("chrome_manager.backup").error(
                        "实例 %s 的恢复中断，数据目录缺失；已保持维护状态供下次启动回滚", profile.id
                    )

    def recover_interrupted(self) -> None:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT id, name, user_data_dir FROM profiles WHERE status='maintenance' AND is_deleted=0"
            ).fetchall()
        for row in rows:
            source = Path(row["user_data_dir"])
            if not source.is_absolute():
                source = self.profiles_root.parent / source
            if not source.exists():
                root = self.backups_root / str(row["id"])
                candidates = sorted(root.glob("*/data"), reverse=True) if root.exists() else []
                for candidate in candidates:
                    try:
                        manifest = json.loads((candidate.parent / "manifest.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if (manifest.get("profile_id") == row["id"] and manifest.get("profile_name") == row["name"]
                            and manifest.get("kind") == "pre-restore"):
                        candidate.rename(source)
                        logging.getLogger("chrome_manager.backup").warning(
                            "实例 %s 的中断恢复已回滚到原数据", row["id"]
                        )
                        break
            if source.exists():
                with self.database.transaction() as connection:
                    connection.execute("UPDATE profiles SET status='stopped' WHERE id=? AND status='maintenance'", (row["id"],))
            else:
                logging.getLogger("chrome_manager.backup").error(
                    "实例 %s 的数据目录缺失，已保持维护状态，请人工检查备份", row["id"]
                )
