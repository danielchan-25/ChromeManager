"""Isolated real Linux Chrome tests. Never use production data or public hosts."""
import asyncio
from dataclasses import replace
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import psutil
import pytest

from chrome_manager.config import Settings, ensure_data_directories
from chrome_manager.core.cdp import probe_browser, command
from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.chrome_service import ChromeService, is_port_available
from chrome_manager.core.process_manager import ProcessManager
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.core.relay import CdpRelay
from chrome_manager.db.database import Database

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.environ.get('CHROME_MANAGER_LIVE_TEST') != '1',
                                reason='Opt-in real Linux Chrome integration')


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path, headless=True, startup_timeout=30, relay_enabled=True,
                        sandbox=os.environ.get('CM_TEST_NO_SANDBOX') != '1', relay_bind='127.0.0.1')
    ensure_data_directories(settings)
    db = Database(settings.database_path); db.initialize()
    profiles = ProfileManager(db, tmp_path / 'profiles')
    chrome = ChromeService(db, settings)
    processes = ProcessManager(db, 10, settings)
    loop = asyncio.new_event_loop()
    relay = CdpRelay(db, settings, interval=.1)
    thread = threading.Thread(target=lambda: loop.run_until_complete(relay.run()), daemon=True)
    thread.start()
    try:
        yield settings, db, profiles, chrome, processes, relay
    finally:
        processes.reconcile()
        processes.recover_restarted(profiles.list())
        for p in profiles.list():
            if p.status == 'running':
                processes.stop_profile(p)
        loop.call_soon_threadsafe(relay.stop_event.set)
        thread.join(10)
        assert not thread.is_alive()
        loop.close()


def public_ready(port, pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            return probe_browser(port, 2, pid)
        except Exception:
            time.sleep(.1)
    pytest.fail(f'Public CDP {port} did not become ready')


def create(profiles, name):
    ports = {p.cdp_port for p in profiles.list()}
    port = next(p for p in range(19550, 19900) if p not in ports and is_port_available(p))
    return profiles.create(name, cdp_port=port)


def test_live_multiple_cdp_clients_and_native_health(runtime):
    from playwright.async_api import async_playwright
    settings, db, profiles, chrome, processes, relay = runtime
    a, b = create(profiles, 'one'), create(profiles, 'two')
    pid_a, pid_b = chrome.start(a), chrome.start(b)
    assert public_ready(a.cdp_port, pid_a)['product']
    assert public_ready(b.cdp_port, pid_b)['product']
    monitor = CdpMonitor(db)

    async def exercise():
        async with async_playwright() as pw:
            ca = await pw.chromium.connect_over_cdp(f'http://127.0.0.1:{a.cdp_port}')
            cb = await pw.chromium.connect_over_cdp(f'http://127.0.0.1:{b.cdp_port}')
            pa, pb = await ca.contexts[0].new_page(), await cb.contexts[0].new_page()
            for _ in range(3):
                va, vb, states = await asyncio.gather(pa.evaluate('1+1'), pb.evaluate('2+2'), asyncio.to_thread(monitor.check_once))
                assert (va, vb) == (2, 4)
                assert all(r['state'] == 'ok' for r in states.values())
            await pa.close(); await pb.close()
    asyncio.run(exercise())
    processes.stop_profile(profiles.show(a.name))
    assert profiles.show(a.name).status == 'stopped'
    assert public_ready(b.cdp_port, pid_b)['product']
    assert chrome.start(profiles.show(a.name)) != pid_a


def test_live_public_listener_never_blocks_private_chrome(runtime):
    _, db, profiles, chrome, _, _ = runtime
    p = create(profiles, 'occupied')
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(('127.0.0.1', p.cdp_port)); occupied.listen()
        pid = chrome.start(p)
        with db.read() as c:
            native = c.execute('SELECT cdp_port FROM runtime_sessions WHERE pid=?', (pid,)).fetchone()[0]
        assert native != p.cdp_port
        assert probe_browser(native, 5, pid)['product']


def test_live_autonomous_restart_is_reattached(runtime):
    settings, db, profiles, _, processes, _ = runtime
    # Chrome's desktop restart handler is a headed feature; exercise the actual
    # production mode under Xvfb, rather than expecting headless to relaunch.
    assert os.environ.get('DISPLAY'), 'Run Linux live tests with xvfb-run -a'
    chrome = ChromeService(db, replace(settings, headless=False))
    p = create(profiles, 'restart')
    old = chrome.start(p)
    with db.read() as c:
        native = c.execute('SELECT cdp_port FROM runtime_sessions WHERE pid=?', (old,)).fetchone()[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    targets = json.load(opener.open(f'http://127.0.0.1:{native}/json/list', timeout=3))
    endpoint = next(t['webSocketDebuggerUrl'] for t in targets if t['type'] == 'page')
    command(endpoint, native, 'Page.navigate', timeout=3, params={'url': 'chrome://settings/'})
    for _ in range(40):
        result = command(endpoint, native, 'Runtime.evaluate', timeout=2,
                         params={'expression': "typeof chrome.send === 'function'", 'returnByValue': True})
        if result.get('result', {}).get('value') is True:
            break
        time.sleep(.1)
    else:
        pytest.fail('Settings did not initialize')
    command(endpoint, native, 'Runtime.evaluate', timeout=2,
            params={'expression': "setTimeout(() => chrome.send('restart'), 100); true", 'userGesture': True})
    for _ in range(100):
        processes.reconcile(); processes.recover_restarted(profiles.list())
        with db.read() as c:
            row = c.execute('SELECT pid FROM runtime_sessions WHERE stopped_at IS NULL ORDER BY id DESC LIMIT 1').fetchone()
        if row and row['pid'] != old:
            new = row['pid']
            break
        time.sleep(.2)
    else:
        for path in (settings.data_root / 'logs/profiles' / str(p.id)).glob('chrome*.log'):
            print(path.name, path.read_text(encoding='utf-8', errors='replace')[-6000:])
        pytest.fail('Chrome replacement process was not adopted')
    assert public_ready(p.cdp_port, new)['product']
    assert profiles.show(p.name).status == 'running'
