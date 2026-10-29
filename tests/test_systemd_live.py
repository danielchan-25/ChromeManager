"""Opt-in isolated real user units with a unique prefix and fresh test data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import urllib.request

import httpx
import pytest
import websocket

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('CHROME_MANAGER_SYSTEMD_TEST') != '1',
    reason='Requires explicitly enabled isolated Linux user systemd test')


def test_real_systemd_web_restart_preserves_browser_and_cdp_connection(tmp_path):
    from chrome_manager.config import Settings, ensure_data_directories
    from chrome_manager.core.profile_manager import ProfileManager
    from chrome_manager.db.database import Database

    root = tmp_path / 'data-root'
    settings = Settings(root)
    ensure_data_directories(settings)
    settings.config_path.write_text('[browser]\nheadless=true\nsandbox=' + ('false' if os.environ.get('CM_TEST_NO_SANDBOX') == '1' else 'true') + '\n[process]\nruntime="systemd"\n[relay]\nenabled=true\nbind_address="127.0.0.1"\n[cdp]\nbind_address="127.0.0.1"\nstartup_timeout=30\n', encoding='utf-8')
    db = Database(settings.database_path); db.initialize()
    profiles = ProfileManager(db, root / 'profiles')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); web_port = s.getsockname()[1]
    p = profiles.create('systemd-ci', cdp_port=19620)
    prefix = f'chromemanager-ci-{uuid.uuid4().hex[:8]}'
    web, relay, browser = f'{prefix}-web.service', f'{prefix}-relay.service', f'{prefix}-browser@{p.id}.service'
    template = f'{prefix}-browser@.service'
    folder = Path.home() / '.config/systemd/user'
    folder.mkdir(parents=True, exist_ok=True)
    common = (f'[Service]\nType=exec\nWorkingDirectory={Path(__file__).resolve().parents[1]}\n'
              f'Environment="CHROME_MANAGER_DATA_ROOT={root}"\n'
              f'Environment="CHROME_MANAGER_UNIT_PREFIX={prefix}"\n'
              'Environment=PYTHONUNBUFFERED=1\nTimeoutStopSec=20\n')
    units = {web: common + f'ExecStart={sys.executable} -m chrome_manager.cli web --port {web_port}\n',
             relay: common + f'ExecStart={sys.executable} -m chrome_manager.cli relay\n',
             template: common + f'ExitType=cgroup\nExecStart={sys.executable} -m chrome_manager.cli browser-run %i\n'}
    def ctl(*args):
        return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=45, check=True)

    def wait_url(client, path, *, ready=False):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                r = client.get(path)
                if r.status_code == 200 and (not ready or r.json().get('ready') is True):
                    return r
            except httpx.HTTPError:
                pass
            time.sleep(.25)
        pytest.fail(f'{path} did not become ready')

    connection = None
    try:
        for name, text in units.items():
            (folder / name).write_text(text, encoding='utf-8')
        ctl('daemon-reload')
        ctl('start', relay, web)
        with httpx.Client(base_url=f'http://127.0.0.1:{web_port}', trust_env=False, timeout=35, follow_redirects=False) as client:
            wait_url(client, '/version')
            response = client.post(f'/profiles/{p.name}/start')
            assert response.status_code == 303 and 'error=' not in response.headers.get('location', '')
            # Monitor runs every 60s by default; native CDP is checked separately
            # below, without delaying this lifecycle check for an entire period.
            with db.read() as c:
                original = c.execute('SELECT pid FROM runtime_sessions WHERE stopped_at IS NULL').fetchone()[0]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(40):
                try:
                    version = json.load(opener.open(f'http://127.0.0.1:{p.cdp_port}/json/version', timeout=2))
                    break
                except OSError:
                    time.sleep(.2)
            else:
                print('relay status', (root / 'data/cdp-lan-status.json').read_text())
                for path in (root / 'logs/relay').glob('*.jsonl'):
                    print('relay diagnostics', path.read_text()[-5000:])
                import psutil
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    if str(root) in ' '.join(proc.info['cmdline'] or []):
                        print('systemd process', proc.info)
                pytest.fail('Systemd relay did not bind')
            connection = websocket.create_connection(version['webSocketDebuggerUrl'], timeout=5, suppress_origin=True, http_no_proxy=['127.0.0.1'])
            connection.send(json.dumps({'id': 1, 'method': 'Browser.getVersion'}))
            assert json.loads(connection.recv()).get('result')
            targets = json.load(opener.open(f'http://127.0.0.1:{p.cdp_port}/json/list', timeout=3))
            assert any(t.get('url') == f'http://127.0.0.1:{web_port}/profiles/{p.name}' for t in targets)
            ctl('restart', web)
            wait_url(client, '/version')
            connection.send(json.dumps({'id': 2, 'method': 'Browser.getVersion'}))
            assert json.loads(connection.recv()).get('result')
            with db.read() as c:
                after = c.execute('SELECT pid FROM runtime_sessions WHERE stopped_at IS NULL').fetchone()[0]
            assert after == original
            response = client.post(f'/profiles/{p.name}/stop')
            assert response.status_code == 303 and 'error=' not in response.headers.get('location', '')
            assert profiles.show(p.name).status == 'stopped'
    finally:
        if connection:
            connection.close()
        subprocess.run(['systemctl', '--user', 'stop', web, relay, browser], capture_output=True, timeout=45)
        for name in units:
            (folder / name).unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=15)
