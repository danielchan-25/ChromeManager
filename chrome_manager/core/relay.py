"""Independent, unprivileged TCP relay for registered browser CDP endpoints."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import time

import psutil

from chrome_manager.config import Settings
from chrome_manager.core.linux_runtime import matches_profile
from chrome_manager.db.database import Database
from chrome_manager.db.repository import ProfileRepository
from chrome_manager.utils.audit import event


@dataclass(frozen=True)
class Target:
    public: int
    backend: int
    pid: int


class CdpRelay:
    def __init__(self, database: Database, settings: Settings, *, interval: float = 2):
        self.database = database
        self.settings = settings
        self.interval = interval
        self.servers: dict[int, tuple[Target, asyncio.Server]] = {}
        self.connections: set[asyncio.Task] = set()
        self.stop_event = asyncio.Event()
        self.last_errors: dict[str, str] = {}

    def desired(self) -> tuple[dict[int, Target], dict[str, str]]:
        with self.database.read() as db:
            profiles = ProfileRepository(self.settings.data_root).list_active(db)
            sessions = {r['profile_id']: r for r in db.execute(
                'SELECT profile_id,pid,process_create_time,cdp_port FROM runtime_sessions WHERE stopped_at IS NULL ORDER BY id')}
        desired, errors = {}, {}
        for p in profiles:
            if p.status != 'running':
                continue
            r = sessions.get(p.id)
            if not r or not p.cdp_port or r['process_create_time'] is None:
                errors[str(p.cdp_port)] = '运行实例缺少会话记录'
                continue
            backend = r['cdp_port']
            if not isinstance(backend, int) or not 20000 <= backend < 30000 or backend == p.cdp_port:
                errors[str(p.cdp_port)] = '内部端口无效，请通过 Linux 管理器重新启动实例'
                continue
            try:
                process = psutil.Process(r['pid'])
                if not matches_profile(process, p.user_data_dir, backend, r['process_create_time']):
                    errors[str(p.cdp_port)] = '浏览器进程身份不匹配'
                    continue
            except psutil.Error:
                errors[str(p.cdp_port)] = '浏览器进程已退出'
                continue
            desired[p.cdp_port] = Target(p.cdp_port, backend, r['pid'])
        return desired, errors

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: Target):
        task = asyncio.current_task()
        self.connections.add(task)
        upstream = None
        copies = []
        counts = [0, 0]
        try:
            remote_reader, upstream = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', target.backend), 3)

            async def copy(source, destination, index):
                while chunk := await source.read(65536):
                    counts[index] += len(chunk)
                    destination.write(chunk)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()

            copies = [asyncio.create_task(copy(reader, upstream, 0)),
                      asyncio.create_task(copy(remote_reader, writer, 1))]
            done, pending = await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 5)
        except (OSError, TimeoutError) as exc:
            event('relay.connection.error', level=logging.DEBUG, port=target.public,
                  backend_port=target.backend, error_type=type(exc).__name__, error=str(exc))
        finally:
            for copy_task in copies:
                copy_task.cancel()
            if copies:
                await asyncio.gather(*copies, return_exceptions=True)
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 2)
                    except (OSError, TimeoutError):
                        pass
            self.connections.discard(task)
            event('relay.connection.closed', port=target.public, backend_port=target.backend,
                  bytes_to_browser=counts[0], bytes_from_browser=counts[1])

    async def sync(self):
        desired, errors = await asyncio.to_thread(self.desired)
        for port, (target, server) in list(self.servers.items()):
            if desired.get(port) != target:
                server.close()
                await server.wait_closed()
                del self.servers[port]
                event('relay.listener.removed', level=logging.INFO, port=port)
        for port, target in desired.items():
            if port in self.servers:
                continue
            try:
                server = await asyncio.start_server(
                    lambda r, w, target=target: self.handle(r, w, target), self.settings.relay_bind, port)
                self.servers[port] = target, server
                event('relay.listener.started', level=logging.INFO, port=port, backend_port=target.backend)
            except OSError as exc:
                errors[str(port)] = f'{type(exc).__name__}: {exc}'
        for port, error in errors.items():
            if self.last_errors.get(port) != error:
                event('relay.listener.error', level=logging.ERROR, port=port, error=error)
        self.last_errors = errors
        expected = sorted(set(desired) | {int(p) for p in errors if p.isdigit()})
        self.status({'checked_at': int(time.time()), 'service': 'running', 'ports': expected,
                     'error': None, 'errors': errors,
                     'listeners': {str(p): p in self.servers and str(p) not in errors for p in expected}})

    def status(self, payload):
        path = self.settings.data_root / 'data/cdp-lan-status.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)

    async def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    await self.sync()
                except Exception as exc:
                    event('relay.sync.failed', level=logging.ERROR, error=str(exc), exc_info=True)
                    self.status({'checked_at': int(time.time()), 'error': f'{type(exc).__name__}: {exc}'})
                try:
                    await asyncio.wait_for(self.stop_event.wait(), self.interval)
                except TimeoutError:
                    pass
        finally:
            for _, server in self.servers.values():
                server.close()
                await server.wait_closed()
            for task in list(self.connections):
                task.cancel()
            if self.connections:
                await asyncio.gather(*list(self.connections), return_exceptions=True)
            self.servers.clear()
            self.status({'checked_at': int(time.time()), 'service': 'stopped', 'error': '转发服务已停止', 'listeners': {}})
