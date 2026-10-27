import asyncio
from dataclasses import replace
import json
import socket

from chrome_manager.config import Settings, ensure_data_directories
from chrome_manager.core.relay import CdpRelay, Target
from chrome_manager.db.database import Database


def relay(tmp_path):
    settings = Settings(tmp_path, relay_enabled=True, relay_bind='127.0.0.1')
    ensure_data_directories(settings)
    db = Database(settings.database_path)
    db.initialize()
    return CdpRelay(db, settings, interval=.05)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def cleanup(daemon):
    for _, server in daemon.servers.values():
        server.close()
        await server.wait_closed()
    for task in list(daemon.connections):
        task.cancel()
    if daemon.connections:
        await asyncio.gather(*list(daemon.connections), return_exceptions=True)


def test_relay_preserves_bytes_and_half_close(tmp_path):
    async def exercise():
        received = []
        async def backend(reader, writer):
            data = await reader.read()
            received.append(data)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        upstream = await asyncio.start_server(backend, '127.0.0.1', 0)
        daemon = relay(tmp_path)
        public = free_port()
        target = Target(public, upstream.sockets[0].getsockname()[1], 123)
        daemon.desired = lambda: ({public: target}, {})
        try:
            await daemon.sync()
            reader, writer = await asyncio.open_connection('127.0.0.1', public)
            request = f'GET /json/version HTTP/1.1\r\nHost: 192.168.1.2:{public}\r\n\r\n'.encode()
            writer.write(request)
            await writer.drain()
            writer.write_eof()
            assert (await asyncio.wait_for(reader.read(), 3)).endswith(b'OK')
            assert received == [request]
            writer.close()
            await writer.wait_closed()
        finally:
            await cleanup(daemon)
            upstream.close()
            await upstream.wait_closed()
    asyncio.run(exercise())


def test_one_occupied_port_does_not_break_other_profiles_and_recovers(tmp_path):
    async def exercise():
        daemon = relay(tmp_path)
        occupied = socket.socket()
        occupied.bind(('127.0.0.1', 0)); occupied.listen()
        bad, good = occupied.getsockname()[1], free_port()
        daemon.desired = lambda: ({bad: Target(bad, 20000, 1), good: Target(good, 20001, 2)}, {})
        try:
            await daemon.sync()
            status = json.loads((tmp_path / 'data/cdp-lan-status.json').read_text(encoding='utf-8'))
            assert status['listeners'][str(good)] is True
            assert status['listeners'][str(bad)] is False
            assert str(bad) in status['errors']
            occupied.close()
            await daemon.sync()
            status = json.loads((tmp_path / 'data/cdp-lan-status.json').read_text(encoding='utf-8'))
            assert all(status['listeners'].values()) and not status['errors']
        finally:
            occupied.close()
            await cleanup(daemon)
    asyncio.run(exercise())
