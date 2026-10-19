"""Opt-in Windows integration test, isolated from the user's data and browsers.

Run with CHROME_MANAGER_LIVE_TEST=1. Requires installed Chrome.
"""

import os
import asyncio
import socket
import subprocess
import sys
import time
import json

import httpx
import psutil
import pytest
import websocket
from playwright.async_api import async_playwright

from chrome_manager.core.process_manager import ProcessManager
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.config import Settings
from chrome_manager.core.chrome_service import ChromeService
from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.cdp import command


pytestmark = pytest.mark.skipif(
    os.environ.get('CHROME_MANAGER_LIVE_TEST') != '1' or sys.platform != 'win32',
    reason='Opt-in test starts an isolated Chrome on Windows',
)


def test_live_multiple_clients_monitor_and_stalled_renderer(tmp_path):
    from chrome_manager.core.chrome_service import is_port_available
    database = Database(tmp_path / 'data/chrome_manager.db')
    database.initialize()
    manager = ProfileManager(database, tmp_path / 'profiles')
    service = ChromeService(database, Settings(tmp_path, startup_timeout=25))
    processes = ProcessManager(database, 10)
    ports = [port for port in range(19622, 19722) if is_port_available(port)][:2]
    assert len(ports) == 2
    profiles = [manager.create(f'isolated-{port}', cdp_port=port) for port in ports]
    monitor = CdpMonitor(database)
    try:
        for profile in profiles:
            service.start(profile)

        async def exercise():
            async with async_playwright() as caller_a, async_playwright() as caller_b:
                a = await caller_a.chromium.connect_over_cdp(f'http://127.0.0.1:{ports[0]}', timeout=15000)
                b = await caller_b.chromium.connect_over_cdp(f'http://127.0.0.1:{ports[1]}', timeout=15000)
                page_a = await a.contexts[0].new_page()
                page_b = await b.contexts[0].new_page()
                await page_a.goto('data:text/html,<title>isolated-stall-test</title>')
                await page_b.goto('data:text/html,<title>isolated-healthy-test</title>')
                for _ in range(3):
                    result, value_a, value_b = await asyncio.gather(
                        asyncio.to_thread(monitor.check_once), page_a.evaluate('1+1'), page_b.evaluate('2+2'),
                    )
                    assert all(state['state'] == 'ok' for state in result.values())
                    assert (value_a, value_b) == (2, 4)

                targets = await asyncio.to_thread(monitor._targets, ports[0], 2)
                target = next(t for t in targets if t['url'] == page_a.url)
                with pytest.raises((TimeoutError, websocket.WebSocketTimeoutException)):
                    await asyncio.to_thread(command, target['webSocketDebuggerUrl'], ports[0],
                                            'Runtime.evaluate', timeout=0.5,
                                            params={'expression': 'while (true) {}'})
                result = await asyncio.to_thread(monitor.check_once)
                assert result[profiles[0].id]['state'] == 'failed'
                assert result[profiles[1].id]['state'] == 'ok'
                assert await page_b.evaluate('2+3') == 5
                # Close only our deliberately stalled scratch tab, at the browser level.
                browser_session = await a.new_browser_cdp_session()
                assert (await browser_session.send('Target.closeTarget', {'targetId': target['id']}))['success']
                await browser_session.detach()
                result = await asyncio.to_thread(monitor.check_once)
                assert all(state['state'] == 'ok' for state in result.values())
                await page_b.close()
        asyncio.run(exercise())
        assert all(manager.show(profile.name).status == 'running' for profile in profiles)
    finally:
        for profile in profiles:
            current = manager.show(profile.name)
            if current.status == 'running':
                processes.stop_profile(current)


def test_live_chrome_lifecycle(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        web_port = probe.getsockname()[1]
    # Avoid the ephemeral client-port range when testing CDP startup retries.
    from chrome_manager.core.chrome_service import is_port_available
    cdp_port = next(port for port in range(19522, 19622) if is_port_available(port))
    env = {**os.environ, 'CHROME_MANAGER_DATA_ROOT': str(tmp_path)}
    database = Database(tmp_path / 'data' / 'chrome_manager.db')
    manager = ProfileManager(database, tmp_path / 'profiles')
    processes = ProcessManager(database, 10)
    name = f'profile-{cdp_port}'
    with (tmp_path / 'test-server.log').open('w', encoding='utf-8') as output:
        server = subprocess.Popen(
            [sys.executable, '-m', 'chrome_manager.cli', 'web', '--port', str(web_port)],
            env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{web_port}', trust_env=False, timeout=30) as client:
                for _ in range(50):
                    try:
                        if client.get('/').status_code == 200:
                            break
                    except httpx.ConnectError:
                        time.sleep(0.2)
                else:
                    pytest.fail('Test server did not start')
                assert client.get('/profiles').status_code == 303
                response = client.post('/profiles', data={'port': cdp_port, 'project_name': 'Isolated regression', 'default_url': f'http://127.0.0.1:{web_port}/'})
                assert response.status_code == 303 and 'error=' not in response.headers['location']
                profile = manager.show(name)
                assert profile.status == 'running'
                with database.read() as db:
                    pid = db.execute('SELECT pid FROM runtime_sessions WHERE stopped_at IS NULL').fetchone()[0]
                args = psutil.Process(pid).cmdline()
                assert '--no-proxy-server' in args
                assert '--enable-logging' in args
                assert '--disable-renderer-backgrounding' in args
                assert f'--user-data-dir={profile.user_data_dir}' in args
                pages = httpx.get(f'http://127.0.0.1:{cdp_port}/json/list', trust_env=False).json()
                urls = [item['url'] for item in pages if item['type'] == 'page']
                assert sorted(urls) == sorted([f'http://127.0.0.1:{web_port}/profiles/{name}', f'http://127.0.0.1:{web_port}/'])
                response = client.get(f'/profiles/{name}/edit')
                assert response.status_code == 303 and 'error=' in response.headers['location']
                response = client.post(f'/profiles/{name}/stop')
                assert response.status_code == 303 and 'error=' not in response.headers['location']
                assert manager.show(name).status == 'stopped'
                response = client.post(f'/profiles/{name}/edit', data={'proxy_enabled': 'true', 'proxy_url': 'http://localhost:bad'})
                assert response.status_code == 303 and 'error=' in response.headers['location']
                assert manager.show(name).proxy_url is None
                response = client.post(f'/profiles/{name}/start')
                assert response.status_code == 303 and 'error=' not in response.headers['location']
                with database.read() as db:
                    pid = db.execute('SELECT pid FROM runtime_sessions WHERE stopped_at IS NULL ORDER BY id DESC').fetchone()[0]
                processes._request_close(pid)
                for _ in range(60):
                    samples = client.get('/api/resources').json()['samples']
                    if samples[-1]['instances'].get(str(profile.id), {}).get('status') == 'stopped':
                        break
                    time.sleep(0.25)
                else:
                    pytest.fail('Manually closed Chrome was not reconciled')
                assert manager.show(name).status == 'stopped'
                assert samples[-1]['instances'][str(profile.id)]['status'] == 'stopped'
                response = client.post(f'/profiles/{name}/delete')
                assert response.status_code == 303 and not manager.list()
                assert (tmp_path / 'logs' / 'console.log').stat().st_size > 0
                log = (tmp_path / 'logs' / 'chrome_manager.log').read_text(encoding='utf-8')
                assert 'chrome_manager.lifecycle' in log
                diagnostics = [json.loads(line) for line in (tmp_path / 'logs/diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
                starts = [r for r in diagnostics if r['event'] == 'browser.start.completed']
                assert len(starts) == 2
                for start in starts:
                    steps = [r for r in diagnostics if r.get('operation_id') == start['operation_id']]
                    assert start['request_id'] and start['browser_pid']
                    assert any(r['event'] == 'browser.start.spawn.completed' for r in steps)
                    assert any(r['event'] == 'browser.start.wait_for_cdp.completed' for r in steps)
                    assert any(r['event'] == 'browser.start.record_running.completed' for r in steps)
                assert any(r['event'] == 'browser.stop.completed' for r in diagnostics)
                assert any(r['event'] == 'browser.stop.wait.completed' for r in diagnostics)
                native_log = tmp_path / 'logs/profiles' / str(profile.id) / 'chrome.log'
                assert native_log.stat().st_size > 0
                native_debug = native_log.with_name('chrome_debug.log')
                assert native_debug.stat().st_size > 0
                assert native_debug.with_name('chrome_debug.log.1').stat().st_size > 0
        finally:
            try:
                if database.path.exists():
                    for profile in manager.list():
                        expected = f'--user-data-dir={profile.user_data_dir}'
                        for process in psutil.process_iter(['cmdline']):
                            if expected in (process.info['cmdline'] or []):
                                try:
                                    processes._request_close(process.pid)
                                    process.wait(timeout=10)
                                except psutil.TimeoutExpired:
                                    process.kill()
                                except psutil.NoSuchProcess:
                                    pass
            finally:
                server.terminate()
                server.wait(timeout=10)
