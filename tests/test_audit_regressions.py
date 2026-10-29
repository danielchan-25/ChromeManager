"""Behavioral regressions discovered by the host-wide code audit."""
import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest
from fastapi.testclient import TestClient

from chrome_manager.config import Settings, load_settings
from chrome_manager.exceptions import ConfigurationError, DatabaseError
from chrome_manager.core.backup_manager import BackupManager, BackupError
from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.chrome_service import ChromeService, ChromeStartError
from chrome_manager.core.process_manager import ProcessManager, ProcessStopError
from chrome_manager.core.profile_manager import ProfileManager, ProfileError
from chrome_manager.db.database import Database
from chrome_manager.web import create_app


@pytest.fixture
def profiles(tmp_path):
    db = Database(tmp_path / 'data/chrome_manager.db')
    db.initialize()
    return ProfileManager(db, tmp_path / 'profiles')


def test_auto_port_wraps_after_65535(profiles):
    profiles.create('last', cdp_port=65535)
    assert 9500 <= profiles.suggested_port() <= 9999
    assert 9500 <= profiles.create('automatic').cdp_port <= 9999


def test_profile_creation_failure_leaves_no_orphan_directory(profiles, monkeypatch):
    monkeypatch.setattr(profiles.repository, 'create', Mock(side_effect=sqlite3.IntegrityError('conflict')))
    with pytest.raises(ProfileError):
        profiles.create('failed', cdp_port=9519)
    assert not (profiles.profiles_root / 'failed/User Data').exists()
    assert profiles.list() == []


def test_deleted_snapshot_cannot_launch_browser(profiles, tmp_path, monkeypatch):
    p = profiles.create('deleted', cdp_port=9519)
    profiles.delete(p.name)
    service = ChromeService(profiles.database, Settings(tmp_path, headless=True))
    monkeypatch.setattr(service, 'arguments', lambda *a: ['/bin/true'])
    monkeypatch.setattr(service, '_wait_for_cdp', lambda *a: None)
    spawn = Mock(return_value=Mock(pid=psutil.Process().pid))
    monkeypatch.setattr('chrome_manager.core.chrome_service.subprocess.Popen', spawn)
    with pytest.raises(ChromeStartError):
        service.start(p)
    spawn.assert_not_called()


def test_start_uses_current_metadata_not_stale_snapshot(profiles, tmp_path, monkeypatch):
    p = profiles.create('fresh', cdp_port=9519)
    profiles.update(p.name, project_name=None, platform=None, account_name=None,
                    description=None, default_url='https://example.org', proxy_url=None)
    service = ChromeService(profiles.database, Settings(tmp_path, headless=True))
    observed = []
    def arguments(profile, port):
        observed.append(profile.default_url)
        return ['/bin/true']
    monkeypatch.setattr(service, 'arguments', arguments)
    monkeypatch.setattr(service, '_wait_for_cdp', lambda *a: None)
    monkeypatch.setattr('chrome_manager.core.chrome_service.subprocess.Popen', Mock(return_value=Mock(pid=psutil.Process().pid)))
    service.start(p)
    assert observed == ['https://example.org']


def test_stop_claim_blocks_concurrent_stop_and_keeps_state(profiles, monkeypatch):
    p = profiles.create('stopping', cdp_port=9519)
    process = Mock(pid=123)
    process.name.return_value = 'chrome'
    process.cmdline.return_value = [f'--user-data-dir={p.user_data_dir}', '--remote-debugging-port=9519']
    process.create_time.return_value = 1.0
    with profiles.database.transaction() as db:
        db.execute("UPDATE profiles SET status='running' WHERE id=?", (p.id,))
        db.execute('INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port) VALUES (?,123,1,9519)', (p.id,))
    monkeypatch.setattr('chrome_manager.core.process_manager.psutil.Process', lambda *a: process)
    manager = ProcessManager(profiles.database, 1)
    def close(pid):
        assert profiles.show(p.name).status == 'stopping'
        with pytest.raises(ProcessStopError):
            manager.stop_profile(p)
        manager.reconcile()
        assert profiles.show(p.name).status == 'stopping'
    monkeypatch.setattr(manager, '_request_close', close)
    manager.stop_profile(p)
    assert profiles.show(p.name).status == 'stopped'


def test_failed_stop_releases_claim_without_signalling_wrong_process(profiles, monkeypatch):
    p = profiles.create('wrong-process', cdp_port=9519)
    with profiles.database.transaction() as db:
        db.execute("UPDATE profiles SET status='running' WHERE id=?", (p.id,))
        db.execute('INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port) VALUES (?,123,1,9519)', (p.id,))
    process = Mock()
    process.name.return_value = 'python3'
    monkeypatch.setattr('chrome_manager.core.process_manager.psutil.Process', lambda *a: process)
    manager = ProcessManager(profiles.database, 1)
    with pytest.raises(ProcessStopError):
        manager.stop_profile(p)
    assert profiles.show(p.name).status == 'running'
    process.kill.assert_not_called()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux Chrome singleton symlink')
def test_backup_rejects_rewritten_live_chrome_title(profiles, monkeypatch):
    p = profiles.create('live title', cdp_port=9519)
    directory = Path(p.user_data_dir)
    (directory / 'SingletonLock').symlink_to(f'{socket.gethostname()}-123')
    process = Mock(pid=123)
    args = f'chrome --user-data-dir={directory} --remote-debugging-port=20000 --restart'.split(' ')
    process.info = {'cmdline': args}
    process.cmdline.return_value = args
    monkeypatch.setattr('chrome_manager.core.backup_manager.psutil.process_iter', lambda *a: [process])
    manager = BackupManager(profiles.database, profiles.profiles_root, profiles.profiles_root.parent / 'backups')
    with pytest.raises(BackupError, match='浏览器|Chrome'):
        manager.backup(p)
    assert profiles.show(p.name).status == 'stopped'


@pytest.mark.parametrize('manifest', ['[]', 'null', '42', '"text"'])
def test_corrupt_backup_manifest_does_not_break_listing(profiles, manifest):
    p = profiles.create('backup', cdp_port=9519)
    manager = BackupManager(profiles.database, profiles.profiles_root, profiles.profiles_root.parent / 'backups')
    backup = manager.backup(p)
    (manager._backup_root(p) / backup / 'manifest.json').write_text(manifest)
    assert manager.list(p) == []


@pytest.mark.parametrize('config', [
    '[browser]\nsandbox=0\n', '[browser]\nsandbox="false"\n',
    '[browser]\nheadless="true"\n', 'browser="bad"\n',
    '[process]\nruntime="typo"\n', '[process]\nshutdown_timeout=0\n',
    '[logging]\nmax_size_mb=-1\n', '[logging]\nbackup_count=0\n',
    '[cdp]\nstartup_timeout=-1\n', '[cdp]\nauto_port_start=9999\nauto_port_end=9500\n',
])
def test_invalid_configuration_is_rejected(tmp_path, monkeypatch, config):
    monkeypatch.delenv('CHROME_MANAGER_DATA_ROOT', raising=False)
    folder = tmp_path / 'config'
    folder.mkdir()
    (folder / 'settings.toml').write_text(config)
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path)


def test_failed_migration_is_atomic_and_can_retry(tmp_path, monkeypatch):
    from chrome_manager.db.migrations import v003, v004
    db = Database(tmp_path / 'data/test.db')
    db.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db.path)
    from chrome_manager.db.migrations import v001, v002
    with connection:
        v001.apply(connection)
        v002.apply(connection)
    connection.close()
    original = v004.apply
    monkeypatch.setattr(v004, 'apply', Mock(side_effect=sqlite3.OperationalError('interrupted')))
    with pytest.raises(DatabaseError):
        db.initialize()
    with db.read() as connection:
        assert connection.execute('SELECT version FROM schema_version').fetchone()[0] == 2
        assert 'default_url' not in {r[1] for r in connection.execute('PRAGMA table_info(profiles)')}
    monkeypatch.setattr(v004, 'apply', original)
    db.initialize()
    with db.read() as connection:
        assert connection.execute('SELECT version FROM schema_version').fetchone()[0] == 8


def test_data_url_content_is_not_logged_as_page_label():
    assert 'private-data' not in CdpMonitor._page_label('data:text/html,private-data')
    assert CdpMonitor._page_label('about:blank') == 'about:blank'


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux systemd parser')
def test_install_generated_units_are_accepted_by_systemd(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts/install-user.sh').read_text()
    embedded = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    home = tmp_path / 'user-home'
    (home / '.config/chromemanager').mkdir(parents=True)
    (home / '.config/systemd/user').mkdir(parents=True)
    project = tmp_path / '安装 project'
    (project / 'deploy/systemd').mkdir(parents=True)
    (project / '.venv/bin').mkdir(parents=True)
    (project / '.venv/bin/python').symlink_to(sys.executable)
    import shutil
    for source in (root / 'deploy/systemd').glob('*.service'):
        shutil.copy2(source, project / 'deploy/systemd' / source.name)
    import os
    environment = {**os.environ, 'CM_INSTALL_PROJECT': str(project), 'CHROME_MANAGER_DATA_ROOT': str(tmp_path / 'data-root')}
    bootstrap = f'from pathlib import Path; Path.home = classmethod(lambda cls: Path({str(home)!r}));' + embedded
    subprocess.run([sys.executable, '-c', bootstrap], env=environment, check=True)
    units = list((home / '.config/systemd/user').glob('*.service'))
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    result = subprocess.run(['systemd-analyze', '--user', 'verify', *map(str, units)], capture_output=True, text=True, env={**os.environ, 'XDG_RUNTIME_DIR': str(runtime)})
    assert result.returncode == 0, result.stderr


def test_import_old_schema_without_default_url(tmp_path):
    from chrome_manager.core.migration import import_windows_metadata
    from chrome_manager.db.migrations import v001
    old = tmp_path / 'windows.db'
    connection = sqlite3.connect(old)
    v001.apply(connection)
    connection.execute("INSERT INTO profiles(id,name,user_data_dir) VALUES (1,'old','old')")
    connection.execute("INSERT INTO cdp_ports(port,profile_id) VALUES (9519,1)")
    connection.commit()
    connection.close()
    settings = Settings(tmp_path / 'target')
    assert import_windows_metadata(old, settings) == ['old']


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.invalid'}, {'Origin': 'null'},
    {'Referer': 'https://evil.invalid/form'}, {'Sec-Fetch-Site': 'cross-site'},
    {'Origin': 'http://[malformed'},
])
def test_cross_site_profile_changes_are_rejected(tmp_path, monkeypatch, headers):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    client = TestClient(create_app())
    profiles = ProfileManager(Database(tmp_path / 'data/chrome_manager.db'), tmp_path / 'profiles')
    p = profiles.create('protected', cdp_port=9519)
    response = client.post('/profiles/protected/delete', headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert profiles.show(p.name)
    client.close()


@pytest.mark.parametrize('headers', [{}, {'Origin': 'http://testserver'}, {'Referer': 'http://testserver/'}])
def test_native_and_same_origin_management_stays_available(tmp_path, monkeypatch, headers):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    client = TestClient(create_app())
    manager = ProfileManager(Database(tmp_path / 'data/chrome_manager.db'), tmp_path / 'profiles')
    manager.create('native', cdp_port=9519)
    assert client.post('/profiles/native/delete', headers=headers, follow_redirects=False).status_code == 303
    assert manager.list() == []
    client.close()


def test_delete_disables_only_its_own_autostart(profiles, monkeypatch):
    p = profiles.create('boot', cdp_port=9519)
    profiles.runtime = 'systemd'
    ctl = Mock()
    monkeypatch.setattr('chrome_manager.core.linux_runtime.systemctl', ctl)
    profiles.delete(p.name)
    ctl.assert_called_once_with('disable', f'chromemanager-browser@{p.id}.service')
    assert profiles.list() == []


def test_delete_failure_preserves_metadata(profiles, monkeypatch):
    p = profiles.create('boot-fail', cdp_port=9519)
    profiles.runtime = 'systemd'
    monkeypatch.setattr('chrome_manager.core.linux_runtime.systemctl', Mock(side_effect=OSError('unavailable')))
    with pytest.raises(ProfileError):
        profiles.delete(p.name)
    assert profiles.show(p.name).cdp_port == 9519


def test_custom_auto_port_bounds_are_honored(profiles):
    profiles.auto_port_start = 19420
    profiles.auto_port_end = 19421
    assert profiles.create('bounded-one').cdp_port == 19420
    assert profiles.create('bounded-two').cdp_port == 19421
    with pytest.raises(ProfileError):
        profiles.create('no-room')
    profiles.delete('bounded-one')
    assert profiles.create('reuse-room').cdp_port == 19420


def test_database_initialization_is_safe_for_concurrent_services(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'data/concurrent.db'
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Database(path).initialize(), range(8)))
    with Database(path).read() as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('SELECT version FROM schema_version').fetchone()[0] == 8


def test_full_auto_port_range_keeps_management_page_available(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(ProfileManager, 'suggested_port', Mock(side_effect=ProfileError('full')))
    client = TestClient(create_app())
    assert client.get('/').status_code == 200
    client.close()


@pytest.mark.parametrize('url', ['http://:80', 'http://@', 'https://invalid host/path'])
def test_invalid_default_url_host_is_rejected(url):
    with pytest.raises(ProfileError):
        ProfileManager._validate_default_url(url)
