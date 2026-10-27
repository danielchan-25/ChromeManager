import sqlite3
import pytest

from chrome_manager.config import Settings
from chrome_manager.core.migration import import_windows_metadata
from chrome_manager.core.profile_manager import ProfileManager, ProfileError
from chrome_manager.db.database import Database


def source(root):
    db = Database(root / 'data/chrome_manager.db'); db.initialize()
    profiles = ProfileManager(db, root / 'profiles')
    p = profiles.create('profile-9600', cdp_port=9600, project_name='metadata', default_url='https://example.com')
    with db.transaction() as c:
        c.execute("UPDATE profiles SET status='running' WHERE id=?", (p.id,))
        c.execute('INSERT INTO runtime_sessions(profile_id,pid,cdp_port) VALUES (?,123,9600)', (p.id,))
    return db, profiles


def test_import_creates_fresh_profiles_and_never_reuses_runtime_or_credentials(tmp_path):
    old, _ = source(tmp_path / 'windows')
    settings = Settings(tmp_path / 'linux')
    assert import_windows_metadata(old.path, settings) == ['profile-9600']
    db = Database(settings.database_path)
    p = ProfileManager(db, settings.data_root / 'profiles').show('profile-9600')
    assert p.status == 'stopped' and p.project_name == 'metadata'
    assert p.cdp_port == 9600
    assert not list((settings.data_root / 'profiles/profile-9600/User Data').iterdir())
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM runtime_sessions').fetchone()[0] == 0


def test_import_conflict_rolls_back_metadata_and_new_directories(tmp_path):
    old, profiles = source(tmp_path / 'windows')
    profiles.create('conflict', cdp_port=9601)
    settings = Settings(tmp_path / 'linux')
    db = Database(settings.database_path); db.initialize()
    manager = ProfileManager(db, settings.data_root / 'profiles')
    manager.create('conflict', cdp_port=9602)
    with pytest.raises(ProfileError):
        import_windows_metadata(old.path, settings)
    assert [p.name for p in manager.list()] == ['conflict']
    assert not (settings.data_root / 'profiles/profile-9600').exists()
