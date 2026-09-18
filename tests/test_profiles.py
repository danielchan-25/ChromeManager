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
