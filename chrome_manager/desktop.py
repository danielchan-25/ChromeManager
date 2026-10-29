"""Same-origin access to the managed X11 desktop through a loopback VNC server."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from chrome_manager.config import Settings, load_settings
from chrome_manager.core.profile_manager import ProfileError, ProfileManager


NOVNC_ROOT = Path('/usr/share/novnc')
WS_MAX_MESSAGE_BYTES = 1024 * 1024
VNC_CHUNK_BYTES = 64 * 1024
CONNECT_TIMEOUT = 3
CLOSE_TIMEOUT = 2
LOGGER = logging.getLogger('chrome_manager.desktop')


def _same_origin(origin: str, destination: str) -> bool:
    """Compare an HTTP Origin to the effective origin of a WebSocket URL."""
    try:
        if any(character.isspace() for character in origin):
            return False
        source = urlsplit(origin)
        target = urlsplit(destination)
        target_scheme = {'ws': 'http', 'wss': 'https'}.get(target.scheme)
        if (source.scheme not in {'http', 'https'} or target_scheme is None
                or not source.hostname or not target.hostname
                or source.username is not None or source.password is not None
                or target.username is not None or target.password is not None
                or source.path not in {'', '/'} or source.query or source.fragment):
            return False
        source_port = source.port if source.port is not None else (443 if source.scheme == 'https' else 80)
        target_port = target.port if target.port is not None else (443 if target_scheme == 'https' else 80)
        return (source.scheme, source.hostname, source_port) == (target_scheme, target.hostname, target_port)
    except (TypeError, ValueError):
        return False


class _ProxyClosed(Exception):
    """Terminate both proxy directions with a specific WebSocket close code."""

    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason


async def _proxy(websocket: WebSocket, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> tuple[int, str]:
    """Forward binary RFB data with bounded chunks and transport backpressure."""
    async def from_browser() -> None:
        while True:
            message = await websocket.receive()
            if message['type'] == 'websocket.disconnect':
                return
            payload = message.get('bytes')
            if payload is None:
                raise _ProxyClosed(1003, '远程桌面仅接受二进制消息')
            if len(payload) > WS_MAX_MESSAGE_BYTES:
                raise _ProxyClosed(1009, '远程桌面消息过大')
            for offset in range(0, len(payload), VNC_CHUNK_BYTES):
                writer.write(payload[offset:offset + VNC_CHUNK_BYTES])
                await writer.drain()

    async def to_browser() -> None:
        while payload := await reader.read(VNC_CHUNK_BYTES):
            await websocket.send_bytes(payload)

    tasks = [asyncio.create_task(from_browser()), asyncio.create_task(to_browser())]
    try:
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
        return 1000, '远程桌面连接已关闭'
    except _ProxyClosed as exc:
        return exc.code, exc.reason
    except WebSocketDisconnect:
        return 1000, '远程桌面连接已关闭'
    except (OSError, RuntimeError):
        LOGGER.warning('远程桌面转发连接中断', exc_info=True)
        return 1011, '远程桌面连接中断，请重新连接'
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def mount_desktop(app: FastAPI, settings: Settings, profiles: ProfileManager, *,
                  templates: Jinja2Templates | None = None) -> None:
    """Mount the viewer and a fixed loopback VNC proxy without browser lifecycle changes."""
    renderer = templates or Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))
    assets_available = NOVNC_ROOT.is_dir()
    if settings.desktop_enabled and assets_available:
        # Distribution noVNC packages may link vendor modules to /usr/share/javascript.
        app.mount('/desktop-assets', StaticFiles(directory=str(NOVNC_ROOT), follow_symlink=True),
                  name='desktop-assets')

    @app.get('/profiles/{name}/browser', response_class=HTMLResponse)
    def browser(name: str, request: Request) -> HTMLResponse:
        try:
            profile = profiles.show(name)
        except ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        display_name = f"{profile.project_name or '未命名项目'} + {profile.platform or '未指定平台'}"
        return renderer.TemplateResponse(request=request, name='desktop.html', context={
            'request': request,
            'profile': profile,
            'display_name': display_name,
            'desktop_enabled': settings.desktop_enabled,
            'desktop_assets_available': assets_available,
            'desktop_headless': settings.headless,
        }, headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'SAMEORIGIN',
                    'Referrer-Policy': 'same-origin'})

    @app.websocket('/profiles/{name}/desktop/socket')
    async def desktop_socket(websocket: WebSocket, name: str) -> None:
        origins = websocket.headers.getlist('origin')
        if len(origins) != 1 or not _same_origin(origins[0], str(websocket.url)):
            await websocket.close(code=1008, reason='请从同源管理页面连接远程桌面')
            return
        if not settings.desktop_enabled or settings.headless or not assets_available:
            await websocket.close(code=1008, reason='远程桌面尚未启用或资源不可用')
            return
        try:
            profile = await run_in_threadpool(profiles.show, name)
        except ProfileError:
            await websocket.close(code=1008, reason='实例不存在')
            return
        if profile.status != 'running':
            await websocket.close(code=1008, reason='请先启动该浏览器实例')
            return
        if type(settings.desktop_port) is not int or not 1 <= settings.desktop_port <= 65535:
            await websocket.close(code=1008, reason='远程桌面服务端口配置无效')
            return

        subprotocols = websocket.scope.get('subprotocols', [])
        await websocket.accept(subprotocol='binary' if 'binary' in subprotocols else None)
        writer = None
        close_code, close_reason = 1000, '远程桌面连接已关闭'
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', settings.desktop_port), CONNECT_TIMEOUT)
            except (OSError, TimeoutError):
                LOGGER.warning('无法连接远程桌面服务（实例 %s）', profile.id)
                close_code, close_reason = 1013, '远程桌面服务暂不可用，请稍后重试'
            else:
                LOGGER.info('远程桌面连接已建立（实例 %s）', profile.id)
                close_code, close_reason = await _proxy(websocket, reader, writer)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT)
                except (OSError, TimeoutError):
                    pass
            if (websocket.client_state != WebSocketState.DISCONNECTED
                    and websocket.application_state == WebSocketState.CONNECTED):
                try:
                    await websocket.close(code=close_code, reason=close_reason)
                except (OSError, RuntimeError, WebSocketDisconnect):
                    pass

    @app.post('/profiles/{name}/desktop/clipboard')
    def clipboard(name: str, text: str = Form(..., max_length=65536)) -> dict[str, bool]:
        # The HTTP management middleware enforces the same-origin mutation policy.
        # X11 UTF8_STRING preserves Chinese/emoji even with legacy VNC clipboard peers.
        if not settings.desktop_enabled or settings.headless or not assets_available:
            raise HTTPException(status_code=503, detail='远程桌面尚未启用或资源不可用')
        try:
            profile = profiles.show(name)
        except ProfileError as exc:
            raise HTTPException(status_code=404, detail='实例不存在') from exc
        if profile.status != 'running':
            raise HTTPException(status_code=409, detail='请先启动该浏览器实例')
        if not text or '\x00' in text:
            raise HTTPException(status_code=400, detail='请输入有效文字')
        try:
            subprocess.run(['xclip', '-selection', 'clipboard', '-in', '-target', 'UTF8_STRING'],
                           input=text.encode('utf-8'), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=3)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning('桌面文字发送失败（实例 %s，%s）', profile.id, type(exc).__name__)
            raise HTTPException(status_code=503, detail='文字发送失败，请检查桌面服务及 xclip 组件后重试') from exc
        return {'ready': True}


def main() -> int:
    """Run the loopback-only desktop service using the same persisted settings."""
    settings = load_settings()
    if not settings.desktop_enabled or settings.headless:
        return 0
    if shutil.which('x11vnc') is None:
        print('远程桌面启动失败：未安装 x11vnc，请先安装该依赖。', file=sys.stderr)
        return 1
    display = os.environ.get('DISPLAY', '').strip()
    authority = os.environ.get('XAUTHORITY', '').strip()
    if not display or not authority:
        print('远程桌面启动失败：需要 DISPLAY 和 XAUTHORITY 环境变量。', file=sys.stderr)
        return 1
    if not Path(authority).is_file() or not os.access(authority, os.R_OK):
        print('远程桌面启动失败：XAUTHORITY 认证文件不存在或不可读。', file=sys.stderr)
        return 1
    if type(settings.desktop_port) is not int or not 1 <= settings.desktop_port <= 65535:
        print('远程桌面启动失败：desktop.port 必须是 1–65535 的整数。', file=sys.stderr)
        return 1
    arguments = ['x11vnc', '-display', display, '-auth', authority, '-localhost',
                 '-rfbport', str(settings.desktop_port), '-forever', '-shared', '-nopw',
                 '-xkb', '-quiet']
    try:
        os.execvp(arguments[0], arguments)
    except OSError as exc:
        print(f'远程桌面启动失败：无法运行 x11vnc：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
