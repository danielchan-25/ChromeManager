from pathlib import Path
from unittest.mock import Mock

from chrome_manager.core.linux_runtime import matches_profile, SystemdProcess


def test_identity_requires_chrome_root_path_port_and_creation_time(tmp_path):
    root = Mock()
    root.name.return_value = 'chrome'
    root.create_time.return_value = 123.0
    args = [f'--user-data-dir={tmp_path}', '--remote-debugging-port=20000']
    root.cmdline.return_value = args
    assert matches_profile(root, str(tmp_path), 20000, 123.0)
    assert not matches_profile(root, str(tmp_path), 20000, 100)
    assert not matches_profile(root, str(tmp_path / 'other'), 20000, 123.0)
    assert not matches_profile(root, str(tmp_path), 20001, 123.0)
    root.cmdline.return_value = [*args, '--type=renderer']
    assert not matches_profile(root, str(tmp_path), 20000, 123.0)
    root.cmdline.return_value = args
    root.name.return_value = 'python3'
    assert not matches_profile(root, str(tmp_path), 20000, 123.0)


def test_systemd_targets_only_the_requested_instance(monkeypatch):
    call = Mock(side_effect=['', '123', ''])
    monkeypatch.setattr('chrome_manager.core.linux_runtime.systemctl', call)
    monkeypatch.setattr('chrome_manager.core.linux_runtime.psutil.Process', Mock())
    process = SystemdProcess(7)
    assert process.pid == 123
    process.terminate()
    assert call.call_args_list[0].args == ('start', 'chromemanager-browser@7.service')
    assert call.call_args_list[-1].args == ('stop', 'chromemanager-browser@7.service')


def test_web_and_browsers_have_separate_service_units():
    root = Path(__file__).resolve().parents[1] / 'deploy/systemd'
    web = (root / 'chromemanager-web.service').read_text()
    browser = (root / 'chromemanager-browser@.service').read_text()
    assert 'browser-run' not in web
    assert 'PartOf=chromemanager-web' not in browser
    assert 'KillMode=control-group' in browser
    assert 'ExitType=cgroup' in browser
