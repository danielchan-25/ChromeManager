"""Opt-in isolated X11/noVNC tests, including real pointer and Unicode input."""

import json
import os
from pathlib import Path
import secrets
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from urllib.parse import quote
import urllib.request

import psutil
import pytest

from chrome_manager.config import ensure_data_directories, load_settings
from chrome_manager.core.cdp import command, probe_browser
from chrome_manager.core.chrome_service import ChromeService
from chrome_manager.core.process_manager import ProcessManager
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database


pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('CHROME_MANAGER_LIVE_TEST') != '1',
    reason='Opt-in real Linux X11/noVNC integration',
)


def _free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def _wait_until(predicate, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    pytest.fail(f'Timed out waiting for {description}')


@pytest.fixture
def desktop_runtime(tmp_path, monkeypatch):
    """Own every display, socket, browser and data directory used by the test."""
    for executable in ('Xvfb', 'xauth', 'openbox', 'x11vnc', 'wmctrl', 'xclip'):
        if not shutil.which(executable):
            pytest.skip(f'Real desktop test requires {executable}')
    chrome_path = next((shutil.which(name) for name in
                        ('google-chrome-stable', 'google-chrome', 'chromium')
                        if shutil.which(name)), None)
    if chrome_path is None or not Path('/usr/share/novnc/core/rfb.js').is_file():
        pytest.skip('Real desktop test requires Chrome and distribution noVNC assets')
    if os.geteuid() == 0 and os.environ.get('CM_TEST_NO_SANDBOX') != '1':
        pytest.skip('Root live tests require explicit CM_TEST_NO_SANDBOX=1')

    root = Path(__file__).resolve().parents[1]
    home = tmp_path / 'home'
    home.mkdir()
    data = tmp_path / 'application'
    # Keep Chrome's Unix socket paths below the kernel limit even with long
    # pytest case names, while retaining IPC files inside the task directory.
    browser_tmp = Path(tempfile.mkdtemp(prefix='t-', dir=tmp_path.parents[1]))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMPDIR', str(browser_tmp))
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(data))
    monkeypatch.setenv('LC_ALL', 'C.UTF-8')
    authority = tmp_path / 'Xauthority'
    cookie = secrets.token_hex(16)
    subprocess.run(['xauth', '-f', str(authority), 'add', ':0', '.', cookie],
                   check=True, capture_output=True)
    monkeypatch.setenv('XAUTHORITY', str(authority))

    children, handles = [], []
    manager = profiles = None

    def spawn(name, arguments, **kwargs):
        log = (tmp_path / f'{name}.log').open('wb')
        handles.append(log)
        child = subprocess.Popen(arguments, cwd=root, env=dict(os.environ),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, **kwargs)
        children.append(child)
        return child

    try:
        # Xvfb chooses an unused display atomically; never reuse production :99.
        xlog = (tmp_path / 'xvfb.log').open('wb')
        handles.append(xlog)
        display = subprocess.Popen(
            ['Xvfb', '-displayfd', '1', '-screen', '0', '1280x960x24',
             '-nolisten', 'tcp', '-auth', str(authority)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=xlog,
            start_new_session=True,
        )
        children.append(display)
        with selectors.DefaultSelector() as selector:
            selector.register(display.stdout, selectors.EVENT_READ)
            assert selector.select(10), 'Xvfb did not allocate its isolated display'
            number = display.stdout.readline().decode().strip()
        assert number.isdecimal(), (tmp_path / 'xvfb.log').read_text()
        display.stdout.close()
        monkeypatch.setenv('DISPLAY', f':{number}')
        subprocess.run(['xauth', '-f', str(authority), 'add', f':{number}', '.', cookie],
                       check=True, capture_output=True)
        spawn('openbox', ['openbox', '--sm-disable'])

        def window_manager_ready():
            result = subprocess.run(['wmctrl', '-m'], capture_output=True, text=True)
            return result.returncode == 0 and 'Openbox' in result.stdout

        _wait_until(window_manager_ready, 'isolated Openbox')
        web_port, desktop_port, cdp_port = _free_port(), _free_port(), _free_port()
        while len({web_port, desktop_port, cdp_port}) != 3:
            cdp_port = _free_port()
            desktop_port = _free_port()
        (data / 'config').mkdir(parents=True)
        sandbox = os.environ.get('CM_TEST_NO_SANDBOX') != '1'
        (data / 'config/settings.toml').write_text(
            f'[browser]\nheadless = false\nsandbox = {str(sandbox).lower()}\n'
            '[cdp]\nbind_address = "127.0.0.1"\nstartup_timeout = 30\n'
            f'[desktop]\nenabled = true\nport = {desktop_port}\n',
            encoding='utf-8',
        )
        settings = load_settings()
        ensure_data_directories(settings)
        database = Database(settings.database_path)
        database.initialize()
        profiles = ProfileManager(database, data / 'profiles')
        profile = profiles.create('desktop-live', cdp_port=cdp_port)
        manager = ProcessManager(database, 10, settings)
        spawn('desktop', [sys.executable, '-m', 'chrome_manager.desktop'])
        spawn('web', [sys.executable, '-c',
                      'import sys, uvicorn; from chrome_manager.web import create_app; '
                      'port=int(sys.argv[1]); '
                      'uvicorn.run(create_app(port), host="127.0.0.1", port=port)',
                      str(web_port)])
        base_url = f'http://127.0.0.1:{web_port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def web_ready():
            try:
                with opener.open(base_url + '/health', timeout=1) as response:
                    return response.status == 200
            except (OSError, urllib.error.URLError):
                return False

        _wait_until(web_ready, 'isolated management service')
        chrome = ChromeService(database, settings, web_port)
        pid = chrome.start(profile)
        with database.read() as connection:
            cdp_port = connection.execute(
                'SELECT cdp_port FROM runtime_sessions WHERE pid=? AND stopped_at IS NULL',
                (pid,),
            ).fetchone()[0]
        yield {'base_url': base_url, 'profile': profile, 'pid': pid,
               'cdp_port': cdp_port, 'chrome_path': chrome_path, 'logs': tmp_path}
    finally:
        # Close only profiles recorded in this fixture's private database.
        try:
            if manager is not None and profiles is not None:
                manager.reconcile()
                for profile in profiles.list():
                    if profile.status == 'running':
                        manager.stop_profile(profile)
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
            for handle in handles:
                handle.close()
            shutil.rmtree(browser_tmp)


def _canvas_colour_bounds(canvas, rgb):
    return canvas.evaluate('''(canvas, rgb) => {
        const {data, width, height} = canvas.getContext('2d').getImageData(
            0, 0, canvas.width, canvas.height);
        let left = width, right = -1, top = height, bottom = -1, count = 0;
        for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
            const offset = (y * width + x) * 4;
            if (data[offset] === rgb[0] && data[offset+1] === rgb[1]
                && data[offset+2] === rgb[2]) {
                left = Math.min(left, x); right = Math.max(right, x);
                top = Math.min(top, y); bottom = Math.max(bottom, y); count++;
            }
        }
        return {left, right, top, bottom, count, width, height};
    }''', rgb)


def _click_remote_colour(viewer, rgb):
    """Locate a test element through actual rendered RFB pixels, then click it."""
    canvas = viewer.locator('#desktop-screen canvas')
    result = None

    def rendered():
        nonlocal result
        result = _canvas_colour_bounds(canvas, rgb)
        return result['count'] > 1000

    _wait_until(rendered, f'remote framebuffer colour {rgb}')
    bounds = canvas.bounding_box()
    assert bounds and bounds['width'] > 100 and bounds['height'] > 100
    viewer.mouse.click(
        bounds['x'] + (result['left'] + result['right']) / 2 / result['width'] * bounds['width'],
        bounds['y'] + (result['top'] + result['bottom']) / 2 / result['height'] * bounds['height'],
    )


def test_remote_browser_pixels_input_unicode_and_reconnection(desktop_runtime):
    from playwright.sync_api import expect, sync_playwright

    runtime = desktop_runtime
    errors = []

    def step(message):
        with (runtime['logs'] / 'desktop-test-steps.log').open('a', encoding='utf-8') as log:
            log.write(f'{time.strftime("%H:%M:%S")} {message}\n')

    step('Create only a synthetic test target through bounded CDP commands')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get_json(path):
        with opener.open(f'http://127.0.0.1:{runtime["cdp_port"]}{path}', timeout=3) as response:
            return json.load(response)

    browser_socket = get_json('/json/version')['webSocketDebuggerUrl']
    # This is a new synthetic page in an isolated browser, never a user tab.
    html = '''<!doctype html><meta charset="utf-8">
            <title>Remote desktop integration fixture</title>
            <style>body{background:#a1d5f2;margin:0} input,button{position:absolute;
            left:80px;width:420px;height:65px;border:0;font-size:25px}
            input{top:80px;background:#ffd500} button{top:190px;background:#3fd250}</style>
            <input id="remote-input" aria-label="Remote test input" autocomplete="off">
            <button id="remote-button" onclick="this.dataset.clicked='yes'">Click via VNC</button>
        '''
    target_id = command(browser_socket, runtime['cdp_port'], 'Target.createTarget', timeout=5,
                        params={'url': 'data:text/html;charset=utf-8,' + quote(html)})['targetId']
    target_socket = next(target['webSocketDebuggerUrl'] for target in get_json('/json/list')
                         if target['id'] == target_id)

    def evaluate(expression):
        return command(target_socket, runtime['cdp_port'], 'Runtime.evaluate', timeout=3,
                       params={'expression': expression, 'returnByValue': True})['result'].get('value')

    def expect_input(value):
        expression = 'document.querySelector("#remote-input")?.value'
        _wait_until(lambda: evaluate(expression) == value,
                    f'synthetic page input value {value!r}', timeout=10)

    _wait_until(lambda: evaluate('document.readyState') == 'complete', 'synthetic page readiness')
    command(target_socket, runtime['cdp_port'], 'Page.bringToFront', timeout=3)
    with sync_playwright() as pw:
        step('Launch headless viewer controller')
        controller = pw.chromium.launch(
            executable_path=runtime['chrome_path'],
            chromium_sandbox=os.environ.get('CM_TEST_NO_SANDBOX') != '1',
            args=['--no-proxy-server'],
            timeout=20000,
        )
        try:
            console = controller.new_page(viewport={'width': 1500, 'height': 1100})
            console.set_default_timeout(15000)
            console.on('pageerror', lambda error: errors.append(str(error)))
            console.goto(runtime['base_url'])
            form = console.locator('#focus-form')
            expect(form).to_be_visible()
            expect(form).to_have_attribute('target', '_blank')
            assert 'view=true' in form.get_attribute('action')
            with console.expect_popup() as popup:
                console.locator('#focus-button').click()
            viewer = popup.value
            viewer.on('pageerror', lambda error: errors.append(str(error)))
            expect(viewer).to_have_url(
                f'{runtime["base_url"]}/profiles/{runtime["profile"].name}/browser')
            expect(viewer.locator('#desktop-status')).to_contain_text('已连接', timeout=20000)
            step('Viewer connected; test actual rendered pointer and keyboard input')
            _click_remote_colour(viewer, [63, 210, 80])
            _wait_until(lambda: evaluate('document.querySelector("#remote-button").dataset.clicked') == 'yes',
                        'remote pointer click on synthetic button')
            _click_remote_colour(viewer, [255, 213, 0])
            viewer.keyboard.type('remote-input')
            expect_input('remote-input')
            # A browser policy denying fullscreen must not mark a healthy
            # RFB connection disconnected or disable the paste controls.
            viewer.locator('#desktop-screen').evaluate('''(screen) => {
                screen.requestFullscreen = () => Promise.reject(new Error('Test denial'));
            }''')
            viewer.locator('#desktop-fullscreen').click()
            expect(viewer.locator('#desktop-status')).to_contain_text('已连接')
            expect(viewer.locator('#desktop-paste-send')).to_be_enabled()
            expect(viewer.locator('#desktop-error')).to_contain_text('全屏')
            step('Fullscreen denial preserved the live connection and paste controls')
            step('Pointer click and ASCII keyboard input passed; close viewer')
            viewer.close()
            assert psutil.pid_exists(runtime['pid'])
            assert probe_browser(runtime['cdp_port'], 5, runtime['pid'])['product']
            expect_input('remote-input')
            with console.expect_popup() as popup:
                console.locator('#focus-button').click()
            reconnected = popup.value
            reconnected.on('pageerror', lambda error: errors.append(str(error)))
            expect(reconnected.locator('#desktop-status')).to_contain_text('已连接', timeout=20000)
            _click_remote_colour(reconnected, [255, 213, 0])
            reconnected.keyboard.press('End')
            reconnected.keyboard.type('-reconnected')
            expect_input('remote-input-reconnected')
            step('Close/reconnect preserved Chrome PID, CDP and input; test Unicode paste')

            # The viewer's paste affordance must preserve non-Latin text end to end.
            reconnected.locator('#desktop-paste-toggle').click()
            pasted = '中文测试：你好，浏览器 😀'
            reconnected.locator('#desktop-paste-text').fill(pasted)
            reconnected.locator('#desktop-paste-send').click()
            expect_input('remote-input-reconnected' + pasted)
            step('Unicode and emoji paste passed')
            assert psutil.pid_exists(runtime['pid'])
            assert errors == []
        finally:
            controller.close()
