"""Remote desktop behavior without connecting to a managed browser or display."""

from contextlib import contextmanager
from dataclasses import replace
import logging
import socket
import subprocess
from threading import Event, Thread
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from httpx import Headers
import pytest
from starlette.websockets import WebSocketDisconnect

from chrome_manager import desktop
from chrome_manager.config import Settings, load_settings
from chrome_manager.core.process_manager import ProcessManager, ProcessStopError
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.exceptions import ConfigurationError
from chrome_manager.web import create_app


class FakeVnc:
    """A bounded loopback peer that echoes binary data and observes socket cleanup."""

    banner = b'RFB 003.008\n'

    def __init__(self, *, close_after_banner=False):
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.close_after_banner = close_after_banner
        self.connected = Event()
        self.disconnected = Event()
        self.stop = Event()
        self.received = bytearray()
        self.peer = None
        self.thread = Thread(target=self._serve, daemon=True)

    def _serve(self):
        deadline = time.monotonic() + 10
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                try:
                    self.peer, _ = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            if self.peer is None:
                return
            with self.peer:
                self.peer.settimeout(0.1)
                self.connected.set()
                self.peer.sendall(self.banner)
                while not self.close_after_banner and not self.stop.is_set() and time.monotonic() < deadline:
                    try:
                        payload = self.peer.recv(65536)
                    except TimeoutError:
                        continue
                    if not payload:
                        break
                    self.received.extend(payload)
                    self.peer.sendall(payload)
        except OSError:
            # The tests deliberately close either side, including during writes.
            pass
        finally:
            if self.connected.is_set():
                self.disconnected.set()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.peer is not None:
            try:
                self.peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive(), 'Fake VNC socket leaked a worker'


@pytest.fixture
def desktop_env(tmp_path, monkeypatch):
    assets = tmp_path / 'novnc'
    (assets / 'core').mkdir(parents=True)
    (assets / 'core/rfb.js').write_text('export default class RFB {}', encoding='utf-8')
    monkeypatch.setattr(desktop, 'NOVNC_ROOT', assets)
    template_dir = tmp_path / 'templates'
    template_dir.mkdir()
    (template_dir / 'desktop.html').write_text(
        '{{ profile.name }};running={{ profile.status == "running" }};'
        'enabled={{ desktop_enabled }};assets={{ desktop_assets_available }};'
        'headless={{ desktop_headless }}', encoding='utf-8',
    )
    database = Database(tmp_path / 'data/chrome_manager.db')
    database.initialize()
    profiles = ProfileManager(database, tmp_path / 'profiles')
    profile = profiles.create('browser', cdp_port=9519)
    with database.transaction() as connection:
        connection.execute("UPDATE profiles SET status='running' WHERE id=?", (profile.id,))

    @contextmanager
    def client(**overrides):
        settings = replace(Settings(tmp_path, desktop_enabled=True), **overrides)
        app = FastAPI()
        desktop.mount_desktop(app, settings, profiles, templates=Jinja2Templates(directory=template_dir))
        with TestClient(app) as test_client:
            yield test_client

    return client, profiles, assets


def websocket(client, *, headers=None, name='browser', **kwargs):
    if isinstance(headers, list):
        headers = Headers(headers)
    return client.websocket_connect(
        f'/profiles/{name}/desktop/socket',
        headers={'origin': 'http://testserver'} if headers is None else headers,
        **kwargs,
    )


def receive_exact(session, count):
    result = bytearray()
    while len(result) < count:
        result.extend(session.receive_bytes())
    assert len(result) == count
    return bytes(result)


def test_binary_round_trip_and_client_disconnect_close_upstream(desktop_env):
    client_factory, _, _ = desktop_env
    with FakeVnc() as vnc, client_factory(desktop_port=vnc.port) as client:
        with websocket(client, subprotocols=['binary']) as session:
            assert session.accepted_subprotocol == 'binary'
            assert receive_exact(session, len(vnc.banner)) == vnc.banner
            payload = b'\x00\xff\x80browser input\r\n'
            session.send_bytes(payload)
            assert receive_exact(session, len(payload)) == payload
            assert vnc.received == payload
        assert vnc.disconnected.wait(2), 'Browser disconnection left the VNC socket open'


def test_upstream_eof_closes_browser_connection(desktop_env):
    client_factory, _, _ = desktop_env
    with FakeVnc(close_after_banner=True) as vnc, client_factory(desktop_port=vnc.port) as client:
        with websocket(client) as session:
            assert receive_exact(session, len(vnc.banner)) == vnc.banner
            with pytest.raises(WebSocketDisconnect) as closed:
                session.receive_bytes()
            assert closed.value.code == 1000
        assert vnc.disconnected.wait(2)


def test_maximum_allowed_binary_message_round_trips(desktop_env):
    client_factory, _, _ = desktop_env
    payload = bytes(range(256)) * (desktop.WS_MAX_MESSAGE_BYTES // 256)
    assert len(payload) == desktop.WS_MAX_MESSAGE_BYTES
    with FakeVnc() as vnc, client_factory(desktop_port=vnc.port) as client:
        with websocket(client) as session:
            receive_exact(session, len(vnc.banner))
            session.send_bytes(payload)
            assert receive_exact(session, len(payload)) == payload
        assert vnc.disconnected.wait(2)


@pytest.mark.parametrize('payload, close_code', [('not binary', 1003), (b'x' * (1024 * 1024 + 1), 1009)],
                         ids=['text-frame', 'oversized-binary-frame'])
def test_invalid_frames_are_rejected_and_close_tcp(desktop_env, payload, close_code):
    client_factory, _, _ = desktop_env
    with FakeVnc() as vnc, client_factory(desktop_port=vnc.port) as client:
        with websocket(client) as session:
            receive_exact(session, len(vnc.banner))
            if isinstance(payload, str):
                session.send_text(payload)
            else:
                session.send_bytes(payload)
            with pytest.raises(WebSocketDisconnect) as closed:
                session.receive_bytes()
            assert closed.value.code == close_code
        assert vnc.disconnected.wait(2)
        assert not vnc.received


@pytest.mark.parametrize('headers', [
    {}, {'origin': 'null'}, {'origin': 'http://evil.invalid'},
    {'origin': 'https://testserver'}, {'origin': 'http://testserver:8765'},
    {'origin': 'http://user:secret@testserver'}, {'origin': 'http://testserver/other'},
    {'origin': 'http://testserver:broken'},
    [('origin', 'http://testserver'), ('origin', 'http://evil.invalid')],
])
def test_bad_origins_are_rejected_before_any_tcp_connection(desktop_env, monkeypatch, headers):
    client_factory, _, _ = desktop_env
    connect = Mock(side_effect=AssertionError('Untrusted origin attempted to reach VNC'))
    monkeypatch.setattr(desktop.asyncio, 'open_connection', connect)
    with client_factory() as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with websocket(client, headers=headers):
                pytest.fail('Untrusted origin was accepted')
        assert closed.value.code == 1008
    connect.assert_not_called()


@pytest.mark.parametrize('state', ['disabled', 'headless', 'missing-assets', 'missing-profile', 'stopped', 'starting', 'stopping'])
def test_unavailable_desktops_are_rejected_before_any_tcp_connection(desktop_env, monkeypatch, state):
    client_factory, profiles, assets = desktop_env
    overrides = {}
    name = 'browser'
    if state == 'disabled':
        overrides['desktop_enabled'] = False
    elif state == 'headless':
        overrides['headless'] = True
    elif state == 'missing-assets':
        monkeypatch.setattr(desktop, 'NOVNC_ROOT', assets.parent / 'not-installed')
    elif state == 'missing-profile':
        name = 'missing'
    else:
        with profiles.database.transaction() as connection:
            connection.execute('UPDATE profiles SET status=?', (state,))
    connect = Mock(side_effect=AssertionError('Unavailable desktop attempted to reach VNC'))
    monkeypatch.setattr(desktop.asyncio, 'open_connection', connect)
    with client_factory(**overrides) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with websocket(client, name=name):
                pytest.fail('Unavailable desktop was accepted')
        assert closed.value.code == 1008
    connect.assert_not_called()


def test_unreachable_vnc_returns_retryable_error(desktop_env):
    client_factory, _, _ = desktop_env
    with socket.create_server(('127.0.0.1', 0)) as reserved:
        port = reserved.getsockname()[1]
    with client_factory(desktop_port=port) as client:
        with websocket(client) as session:
            with pytest.raises(WebSocketDisconnect) as closed:
                session.receive_bytes()
            assert closed.value.code == 1013


def test_vnc_connection_timeout_cancels_pending_connect(desktop_env, monkeypatch):
    client_factory, _, _ = desktop_env
    cancelled = Event()

    async def stalled_connect(host, port):
        assert host == '127.0.0.1'
        try:
            await desktop.asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(desktop.asyncio, 'open_connection', stalled_connect)
    monkeypatch.setattr(desktop, 'CONNECT_TIMEOUT', 0.03)
    with client_factory() as client:
        with websocket(client) as session:
            with pytest.raises(WebSocketDisconnect) as closed:
                session.receive_bytes()
            assert closed.value.code == 1013
    assert cancelled.is_set(), 'A timed out TCP connection was left running'


@pytest.mark.parametrize('origin, destination', [
    ('http://testserver:80', 'ws://testserver'),
    ('http://testserver', 'ws://testserver:80'),
    ('https://testserver:443', 'wss://testserver'),
    ('https://testserver', 'wss://testserver:443'),
    ('http://TESTSERVER', 'ws://testserver'),
    ('http://[::1]:8765', 'ws://[::1]:8765'),
])
def test_effectively_same_origins_can_connect(desktop_env, origin, destination):
    client_factory, _, _ = desktop_env
    with FakeVnc() as vnc, client_factory(desktop_port=vnc.port) as client:
        with client.websocket_connect(destination + '/profiles/browser/desktop/socket',
                                      headers={'origin': origin}) as session:
            assert receive_exact(session, len(vnc.banner)) == vnc.banner
        assert vnc.disconnected.wait(2)


def test_viewer_and_local_assets_are_available_without_mutating_profile(desktop_env):
    client_factory, profiles, _ = desktop_env
    before = profiles.show('browser')
    with client_factory() as client:
        response = client.get('/profiles/browser/browser')
        assert response.status_code == 200
        assert 'running=True;enabled=True;assets=True;headless=False' in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-frame-options'] == 'SAMEORIGIN'
        assets = client.get('/desktop-assets/core/rfb.js')
        assert assets.status_code == 200
        assert 'export default class RFB' in assets.text
        assert client.get('/profiles/missing/browser').status_code == 404
    assert profiles.show('browser') == before


@pytest.mark.parametrize('overrides, expected', [
    ({'desktop_enabled': False}, 'enabled=False'),
    ({'headless': True}, 'headless=True'),
])
def test_viewer_renders_unavailable_state_instead_of_failing(desktop_env, overrides, expected):
    client_factory, _, _ = desktop_env
    with client_factory(**overrides) as client:
        response = client.get('/profiles/browser/browser')
        assert response.status_code == 200
        assert expected in response.text


@pytest.mark.parametrize('config', [
    'enabled=0', 'enabled="true"', 'port="5900"', 'port=true',
    'port=0', 'port=-1', 'port=65536', 'port=5900.5',
])
def test_invalid_desktop_configuration_is_rejected(tmp_path, monkeypatch, config):
    monkeypatch.delenv('CHROME_MANAGER_DATA_ROOT', raising=False)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_text('[desktop]\n' + config + '\n')
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path)


@pytest.mark.parametrize('port', [1, 5900, 65535])
def test_desktop_configuration_accepts_valid_port_boundaries(tmp_path, monkeypatch, port):
    monkeypatch.delenv('CHROME_MANAGER_DATA_ROOT', raising=False)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_text(f'[desktop]\nenabled=true\nport={port}\n')
    settings = load_settings(tmp_path)
    assert settings.desktop_enabled is True
    assert settings.desktop_port == port


def test_desktop_configuration_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv('CHROME_MANAGER_DATA_ROOT', raising=False)
    settings = load_settings(tmp_path)
    assert settings.desktop_enabled is False
    assert settings.desktop_port == 5900


@pytest.mark.parametrize('headless, enabled', [(False, False), (True, True)])
def test_desktop_service_skips_disabled_and_headless_modes(tmp_path, monkeypatch, headless, enabled):
    monkeypatch.setattr(desktop, 'load_settings', lambda: Settings(tmp_path, headless=headless, desktop_enabled=enabled))
    execute = Mock(side_effect=AssertionError('Inactive desktop must not run x11vnc'))
    monkeypatch.setattr(desktop.os, 'execvp', execute)
    assert desktop.main() == 0
    execute.assert_not_called()


def test_desktop_service_uses_configured_port_and_loopback_binding(tmp_path, monkeypatch):
    authority = tmp_path / 'Xauthority'
    authority.write_bytes(b'fake cookie used only by a mocked exec')
    monkeypatch.setenv('DISPLAY', ':42')
    monkeypatch.setenv('XAUTHORITY', str(authority))
    monkeypatch.setattr(desktop, 'load_settings', lambda: Settings(tmp_path, desktop_enabled=True, desktop_port=5917))
    monkeypatch.setattr(desktop.shutil, 'which', lambda name: '/usr/bin/x11vnc')
    execute = Mock()
    monkeypatch.setattr(desktop.os, 'execvp', execute)
    assert desktop.main() == 0
    command, arguments = execute.call_args.args
    assert command == 'x11vnc'
    assert '-localhost' in arguments
    assert arguments[arguments.index('-rfbport') + 1] == '5917'
    assert arguments[arguments.index('-display') + 1] == ':42'
    assert arguments[arguments.index('-auth') + 1] == str(authority)


@pytest.mark.parametrize('view', [False, True])
def test_focus_feedback_or_viewer_redirect_without_restarting_browser(tmp_path, monkeypatch, view):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    client = TestClient(create_app())
    profiles = ProfileManager(Database(tmp_path / 'data/chrome_manager.db'), tmp_path / 'profiles')
    profile = profiles.create('viewer browser', cdp_port=9519)
    focus = Mock()
    monkeypatch.setattr(ProcessManager, 'focus_profile', focus)
    try:
        response = client.post('/profiles/viewer%20browser/focus', params={'view': str(view).lower()}, follow_redirects=False)
        assert response.status_code == 303
        focus.assert_called_once()
        assert focus.call_args.args[0].name == profile.name
        destination = urlsplit(response.headers['location'])
        if view:
            assert destination.path == '/profiles/viewer%20browser/browser'
        else:
            assert destination.path == '/'
            query = parse_qs(destination.query)
            assert query['selected'] == [profile.name]
            assert query['notice'][0]
    finally:
        client.close()


def test_failed_focus_does_not_open_misleading_viewer(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    client = TestClient(create_app())
    profiles = ProfileManager(Database(tmp_path / 'data/chrome_manager.db'), tmp_path / 'profiles')
    profiles.create('stale', cdp_port=9519)
    monkeypatch.setattr(ProcessManager, 'focus_profile', Mock(side_effect=ProcessStopError('stale browser process')))
    try:
        response = client.post('/profiles/stale/focus?view=true', follow_redirects=False)
        assert response.status_code == 303
        destination = urlsplit(response.headers['location'])
        assert destination.path == '/'
        assert parse_qs(destination.query)['error'] == ['stale browser process']
    finally:
        client.close()


def test_clipboard_preserves_utf8_in_stdin_and_uses_fixed_command(desktop_env, monkeypatch, caplog):
    client_factory, _, _ = desktop_env
    private_text = '中文输入😀\nclipboard-private-canary; $(ignored) --selection primary'
    execute = Mock()
    monkeypatch.setattr(desktop.subprocess, 'run', execute)
    desktop.LOGGER.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=desktop.LOGGER.name), client_factory() as client:
            response = client.post('/profiles/browser/desktop/clipboard', data={'text': private_text})
        assert response.status_code == 200
        assert response.json() == {'ready': True}
        execute.assert_called_once_with(
            ['xclip', '-selection', 'clipboard', '-in', '-target', 'UTF8_STRING'],
            input=private_text.encode('utf-8'), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=True, timeout=3,
        )
        assert private_text not in caplog.text
    finally:
        desktop.LOGGER.removeHandler(caplog.handler)


@pytest.mark.parametrize('failure', ['missing', 'failed', 'timeout'])
def test_clipboard_failure_is_retryable_and_logs_no_private_text(desktop_env, monkeypatch, caplog, failure):
    client_factory, _, _ = desktop_env
    private_text = '剪贴板隐私-canary-do-not-log'
    if failure == 'missing':
        error = FileNotFoundError(private_text)
    elif failure == 'failed':
        error = subprocess.CalledProcessError(1, ['xclip'], output=private_text.encode('utf-8'))
    else:
        error = subprocess.TimeoutExpired(['xclip'], 3, output=private_text.encode('utf-8'))
    execute = Mock(side_effect=error)
    monkeypatch.setattr(desktop.subprocess, 'run', execute)
    desktop.LOGGER.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=desktop.LOGGER.name), client_factory() as client:
            response = client.post('/profiles/browser/desktop/clipboard', data={'text': private_text})
        assert response.status_code == 503
        execute.assert_called_once()
        assert any(record.name == desktop.LOGGER.name and record.levelno >= logging.WARNING
                   for record in caplog.records)
        assert private_text not in caplog.text
        assert private_text not in response.text
    finally:
        desktop.LOGGER.removeHandler(caplog.handler)


@pytest.mark.parametrize('state, expected_status', [
    ('disabled', 503), ('headless', 503), ('missing-assets', 503),
    ('missing-profile', 404), ('stopped', 409), ('starting', 409), ('stopping', 409),
])
def test_clipboard_unavailable_state_never_runs_xclip(desktop_env, monkeypatch, state, expected_status):
    client_factory, profiles, assets = desktop_env
    overrides = {}
    name = 'browser'
    if state == 'disabled':
        overrides['desktop_enabled'] = False
    elif state == 'headless':
        overrides['headless'] = True
    elif state == 'missing-assets':
        monkeypatch.setattr(desktop, 'NOVNC_ROOT', assets.parent / 'not-installed')
    elif state == 'missing-profile':
        name = 'missing'
    else:
        with profiles.database.transaction() as connection:
            connection.execute('UPDATE profiles SET status=?', (state,))
    execute = Mock(side_effect=AssertionError('Unavailable desktop tried to change clipboard'))
    monkeypatch.setattr(desktop.subprocess, 'run', execute)
    with client_factory(**overrides) as client:
        response = client.post(f'/profiles/{name}/desktop/clipboard', data={'text': 'example'})
    assert response.status_code == expected_status
    execute.assert_not_called()


@pytest.mark.parametrize('form, expected_status', [
    ({}, 422), ({'text': ''}, 422), ({'text': 'contains\x00NUL'}, 400),
    ({'text': '中' * 65537}, 422),
], ids=['missing-field', 'empty', 'nul-character', 'oversized-unicode'])
def test_clipboard_invalid_text_never_runs_xclip(desktop_env, monkeypatch, form, expected_status):
    client_factory, _, _ = desktop_env
    execute = Mock(side_effect=AssertionError('Invalid text tried to change clipboard'))
    monkeypatch.setattr(desktop.subprocess, 'run', execute)
    with client_factory() as client:
        response = client.post('/profiles/browser/desktop/clipboard', data=form)
    assert response.status_code == expected_status
    execute.assert_not_called()


def test_clipboard_maximum_unicode_text_is_accepted(desktop_env, monkeypatch):
    client_factory, _, _ = desktop_env
    execute = Mock()
    monkeypatch.setattr(desktop.subprocess, 'run', execute)
    text = '中' * 65536
    with client_factory() as client:
        response = client.post('/profiles/browser/desktop/clipboard', data={'text': text})
    assert response.status_code == 200
    assert execute.call_args.kwargs['input'] == text.encode('utf-8')


@pytest.mark.parametrize('headers', [
    {'Origin': 'http://evil.invalid'}, {'Origin': 'null'},
    {'Referer': 'https://evil.invalid/form'}, {'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_site_clipboard_requests_are_rejected_by_management_app(tmp_path, monkeypatch, headers):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    client = TestClient(create_app())
    execute = Mock(side_effect=AssertionError('Cross-site request tried to change clipboard'))
    monkeypatch.setattr(desktop.subprocess, 'run', execute)
    try:
        response = client.post('/profiles/browser/desktop/clipboard', headers=headers, data={'text': 'private text'})
        assert response.status_code == 403
        execute.assert_not_called()
    finally:
        client.close()
