from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chrome_manager.core.backup_manager import BackupError, BackupManager
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.web import create_app


@pytest.fixture
def prepared(tmp_path):
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    profile = ProfileManager(database, tmp_path / "profiles").create("profile-9510", cdp_port=9510)
    manager = BackupManager(database, tmp_path / "profiles", tmp_path / "backups")
    marker = Path(profile.user_data_dir) / "state.txt"
    marker.write_text("original", encoding="utf-8")
    return database, profile, manager, marker


def test_backup_restore_preserves_previous_data(prepared):
    database, profile, manager, marker = prepared
    backup_id = manager.backup(profile)
    marker.write_text("changed", encoding="utf-8")
    previous_id = manager.restore(profile, backup_id)
    assert marker.read_text(encoding="utf-8") == "original"
    assert (manager._backup_root(profile) / previous_id / "data" / "state.txt").read_text(encoding="utf-8") == "changed"
    assert {item["kind"] for item in manager.list(profile)} == {"manual", "pre-restore"}
    assert ProfileManager(database, manager.profiles_root).show(profile.name).status == "stopped"


def test_backup_accepts_legacy_data_folder_after_display_name_change(prepared):
    _, profile, manager, _ = prepared
    renamed = replace(profile, name="new-display-name")
    assert manager.backup(renamed) in {item["id"] for item in manager.list(renamed)}


def test_restore_rejects_other_backup_and_running_profile(prepared):
    database, profile, manager, marker = prepared
    backup_id = manager.backup(profile)
    with pytest.raises(BackupError, match="不存在"):
        manager.restore(profile, "20260101-000000-deadbeef")
    with database.transaction() as connection:
        connection.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))
    with pytest.raises(BackupError):
        manager.restore(profile, backup_id)
    assert marker.read_text(encoding="utf-8") == "original"


def test_restore_rename_failure_rolls_back(prepared, monkeypatch):
    database, profile, manager, marker = prepared
    backup_id = manager.backup(profile)
    marker.write_text("changed", encoding="utf-8")
    real_rename = Path.rename

    def fail_staged_restore(path, target):
        if path.name.startswith(".restore-"):
            raise OSError("simulated rename failure")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_staged_restore)
    with pytest.raises(BackupError):
        manager.restore(profile, backup_id)
    assert marker.read_text(encoding="utf-8") == "changed"
    assert ProfileManager(database, manager.profiles_root).show(profile.name).status == "stopped"


def test_interrupted_restore_is_recovered_on_startup(prepared):
    database, profile, manager, marker = prepared
    previous = manager._backup_root(profile) / manager._new_id()
    manager._write_manifest(previous, profile, "pre-restore")
    Path(profile.user_data_dir).rename(previous / "data")
    with database.transaction() as connection:
        connection.execute("UPDATE profiles SET status='maintenance' WHERE id=?", (profile.id,))
    manager.recover_interrupted()
    assert marker.read_text(encoding="utf-8") == "original"
    assert ProfileManager(database, manager.profiles_root).show(profile.name).status == "stopped"


def test_page_requires_explicit_restore_confirmation(prepared, tmp_path, monkeypatch):
    database, profile, manager, marker = prepared
    backup_id = manager.backup(profile)
    marker.write_text("changed", encoding="utf-8")
    monkeypatch.setenv("CHROME_MANAGER_DATA_ROOT", str(tmp_path))
    with TestClient(create_app()) as client:
        page = client.get("/")
        assert "备份数据" in page.text and "恢复数据" in page.text
        denied = client.post(f"/profiles/{profile.name}/restore", data={"backup_id": backup_id}, follow_redirects=False)
        assert "error=" in denied.headers["location"]
        assert marker.read_text(encoding="utf-8") == "changed"
        accepted = client.post(f"/profiles/{profile.name}/restore", data={"backup_id": backup_id, "confirmed": "true"}, follow_redirects=False)
        assert "notice=" in accepted.headers["location"]
        assert marker.read_text(encoding="utf-8") == "original"
