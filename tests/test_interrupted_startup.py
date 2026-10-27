from chrome_manager.config import Settings
from chrome_manager.core.process_manager import ProcessManager
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database


def test_manager_restart_recovers_interrupted_start_but_periodic_reconcile_does_not(tmp_path):
    db = Database(tmp_path / 'data/chrome_manager.db'); db.initialize()
    profiles = ProfileManager(db, tmp_path / 'profiles')
    p = profiles.create('interrupted', cdp_port=9600)
    with db.transaction() as c:
        c.execute("UPDATE profiles SET status='starting' WHERE id=?", (p.id,))
    processes = ProcessManager(db, 10)
    processes.reconcile()
    assert profiles.show(p.name).status == 'starting'
    processes.recover_interrupted_states()
    assert profiles.show(p.name).status == 'stopped'


def test_running_metadata_without_session_becomes_stopped(tmp_path):
    db = Database(tmp_path / 'data/chrome_manager.db'); db.initialize()
    profiles = ProfileManager(db, tmp_path / 'profiles')
    p = profiles.create('orphan', cdp_port=9600)
    with db.transaction() as c:
        c.execute("UPDATE profiles SET status='running' WHERE id=?", (p.id,))
    ProcessManager(db, 10).reconcile()
    assert profiles.show(p.name).status == 'stopped'
