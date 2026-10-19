"""Bounded local CDP commands that never attach to or close business tabs."""

import json
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import websocket

from chrome_manager.utils.audit import step
import logging


def local_endpoint(endpoint: str, port: int) -> str:
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'ws' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.port != port or parsed.username or parsed.password):
        raise ValueError('CDP returned an unexpected WebSocket endpoint')
    return endpoint


def command(endpoint: str, port: int, method: str, *, timeout: float,
            params: dict | None = None) -> dict:
    with step('cdp.command', failure_level=logging.DEBUG, port=port, method=method,
              endpoint=endpoint, timeout_seconds=timeout):
        return _command(endpoint, port, method, timeout=timeout, params=params)


def _command(endpoint: str, port: int, method: str, *, timeout: float,
             params: dict | None = None) -> dict:
    deadline = time.monotonic() + timeout
    connection = websocket.create_connection(
        local_endpoint(endpoint, port), timeout=timeout, suppress_origin=True,
        http_no_proxy=['127.0.0.1', 'localhost', '::1'],
    )
    try:
        connection.send(json.dumps({'id': 1, 'method': method, 'params': params or {}}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('CDP command deadline exceeded')
            connection.settimeout(remaining)
            message = json.loads(connection.recv())
            if not isinstance(message, dict) or message.get('id') != 1:
                continue
            if 'error' in message or not isinstance(message.get('result'), dict):
                raise ValueError(f'CDP command failed: {method}: {message.get("error", "invalid result")}')
            return message['result']
    finally:
        with step('cdp.socket.close', failure_level=logging.DEBUG, port=port, method=method):
            connection.close(timeout=0)


def probe_browser(port: int, timeout: float, expected_pid: int | None = None) -> dict:
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('CDP browser deadline exceeded')
        return value

    opener = build_opener(ProxyHandler({}))
    with step('cdp.http.version', failure_level=logging.DEBUG, port=port):
        with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=remaining()) as response:
            payload = json.load(response)
    endpoint = payload['webSocketDebuggerUrl']
    result = command(endpoint, port, 'Browser.getVersion', timeout=remaining())
    if not result.get('product') or not result.get('protocolVersion'):
        raise ValueError('Incomplete CDP browser response')
    if expected_pid is not None:
        processes = command(endpoint, port, 'SystemInfo.getProcessInfo', timeout=remaining())
        if not any(p.get('type') == 'browser' and p.get('id') == expected_pid
                   for p in processes.get('processInfo', [])):
            raise ValueError('CDP port belongs to a different browser process')
    return result
