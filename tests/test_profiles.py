import sqlite3
import shutil
from pathlib import Path

from chrome_manager.core.profile_manager import ProfileError, ProfileManager
from chrome_manager.db.database import Database


def manager(tmp_path: Path) -> ProfileManager:
    database = Database(tmp_path / "data" / "chrome_manager.db")
    database.initialize()
    return ProfileManager(database, tmp_path / "profiles")


def test_create_profile_assigns_isolated_directory_and_port(tmp_path: Path) -> None:
    profiles = manager(tmp_path)
    profile = profiles.create("bili_001", cdp_port=9500, project_name="media")
    assert Path(profile.user_data_dir) == tmp_path / "profiles" / "bili_001" / "User Data"
    assert Path(profile.user_data_dir).is_dir()
    assert profile.cdp_port == 9500
    with profiles.database.read() as connection:
        assert connection.execute("SELECT user_data_dir FROM profiles WHERE id = ?", (profile.id,)).fetchone()[0] == "profiles/bili_001/User Data"


def test_profile_path_follows_relocated_data_root(tmp_path: Path) -> None:
    original = manager(tmp_path / "original")
    profile = original.create("portable", cdp_port=9500)
    (Path(profile.user_data_dir) / "state.txt").write_text("kept", encoding="utf-8")
    moved_root = tmp_path / "new-location"
    moved_root.mkdir()
    (moved_root / "data").mkdir()
    shutil.copytree(original.profiles_root, moved_root / "profiles")
    with original.database.read() as source, sqlite3.connect(moved_root / "data" / "chrome_manager.db") as target:
        source.backup(target)
    relocated = ProfileManager(Database(moved_root / "data" / "chrome_manager.db"), moved_root / "profiles")
    relocated_path = Path(relocated.show("portable").user_data_dir)
    assert relocated_path == moved_root / "profiles" / "portable" / "User Data"
    assert (relocated_path / "state.txt").read_text(encoding="utf-8") == "kept"


def test_profile_rejects_duplicate_port_and_soft_deletes(tmp_path: Path) -> None:
    profiles = manager(tmp_path)
    profiles.create("one", cdp_port=9500)
    try:
        profiles.create("two", cdp_port=9500)
    except ProfileError as exc:
        assert "9500" in str(exc)
    else:
        raise AssertionError("Expected duplicate port to be rejected")
    profiles.delete("one")
    assert profiles.list() == []


def test_profile_auto_assigns_an_available_cdp_port(tmp_path: Path) -> None:
    profile = manager(tmp_path).create("auto_port")
    assert profile.cdp_port is not None
    assert 9500 <= profile.cdp_port <= 9999
