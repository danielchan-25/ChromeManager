"""Local-only FastAPI management console."""

from __future__ import annotations

import os
import logging
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from chrome_manager import __version__
from chrome_manager.config import ensure_data_directories, load_settings, write_default_settings
from chrome_manager.core.chrome_service import ChromeService, ChromeStartError
from chrome_manager.core.backup_manager import BackupError, BackupManager
from chrome_manager.core.cdp_monitor import CdpMonitor
from chrome_manager.core.profile_manager import ProfileError, ProfileManager
from chrome_manager.core.process_manager import ProcessManager, ProcessStopError
from chrome_manager.core.instance_status import StatusSynchronizer
from chrome_manager.db.database import Database
from chrome_manager.desktop import mount_desktop
from chrome_manager.utils.logger import configure_logging
from chrome_manager.utils.audit import event, operation, step
from uuid import uuid4

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def version_info() -> dict[str, object]:
    """Capture deployment identity once, rather than reading Git on each request."""
    project_path = PACKAGE_DIR.resolve().parent
    commit = None
    dirty = None
    if (project_path / ".git").exists():
        try:
            options = {
                "capture_output": True, "text": True, "encoding": "utf-8",
                "errors": "replace", "check": True, "timeout": 2,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            }
            commit = subprocess.run(
                ["git", "-C", str(project_path), "rev-parse", "--verify", "HEAD"], **options,
            ).stdout.strip()
            dirty = bool(subprocess.run(
                ["git", "-C", str(project_path), "status", "--porcelain", "--untracked-files=normal"], **options,
            ).stdout.strip())
        except (OSError, subprocess.SubprocessError):
            commit = None
            dirty = None
    return {
        "name": "ChromeManager", "version": __version__,
        "git_commit": commit, "git_dirty": dirty,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_path": str(project_path), "python_executable": sys.executable,
    }


def same_origin(source: str, destination: str) -> bool:
    def identity(url):
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid HTTP origin')
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        return identity(source) == identity(destination)
    except ValueError:
        return False


def home_redirect(selected: str = "", error: str = "", notice: str = "") -> RedirectResponse:
    """Return to the console while retaining the instance the user was operating."""
    query = []
    if selected:
        query.append(f"selected={quote(selected)}")
    if error:
        query.append(f"error={quote(error)}")
    if notice:
        query.append(f"notice={quote(notice)}")
    return RedirectResponse(url=f"/?{'&'.join(query)}" if query else "/", status_code=303)


def error_redirect(message: str, selected: str = "") -> RedirectResponse:
    """Return user-facing validation failures without exposing a server error page."""
    logging.getLogger('chrome_manager.web').warning('操作失败：%s', message)
    return home_redirect(selected, message)


def detected_local_proxy() -> str | None:
    """Read an opt-in local proxy setting without exposing credentials in the UI."""
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        value = os.environ.get(name)
        if value:
            return value.split("@")[-1] if "@" in value else value
    return None


def sort_profiles(profile_list: list[object], sort_by: str) -> list[object]:
    """Sort the instance list using only presentation-level metadata."""
    sorters = {
        "port": lambda profile: (profile.cdp_port is None, profile.cdp_port or 0, profile.name.casefold()),
        "project": lambda profile: ((profile.project_name or "￿").casefold(), profile.name.casefold()),
        "platform": lambda profile: ((profile.platform or "￿").casefold(), profile.name.casefold()),
        "status": lambda profile: (profile.status, profile.name.casefold()),
    }
    return sorted(profile_list, key=sorters.get(sort_by, sorters["port"]))


def display_profile_name(profile: object) -> str:
    return f"{profile.project_name or '未命名项目'} + {profile.platform or '未指定平台'}"


def runtime_details(database: Database) -> dict[int, dict[str, object]]:
    """Return the current managed process details used by the local console."""
    with database.read() as connection:
        rows = connection.execute(
            "SELECT r.profile_id, r.pid, r.started_at FROM runtime_sessions r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE r.stopped_at IS NULL AND p.status = 'running' AND p.is_deleted = 0 ORDER BY r.id ASC"
        ).fetchall()
    return {
        int(row["profile_id"]): {"pid": row["pid"], "started_at": row["started_at"]}
        for row in rows
    }


def create_app(web_port: int = 8765) -> FastAPI:
    deployment_version = version_info()
    settings = load_settings()
    ensure_data_directories(settings)
    configure_logging(settings.data_root / 'logs', settings.log_max_size_mb, settings.log_backup_count)
    event('manager.initialize', level=logging.INFO, version=deployment_version,
          data_root=str(settings.data_root), web_port=web_port, bind_address=settings.bind_address,
          startup_timeout=settings.startup_timeout, shutdown_timeout=settings.shutdown_timeout)
    with step('manager.prepare_settings'):
        write_default_settings(settings)
    database = Database(settings.database_path)
    with step('manager.initialize_database', database_path=str(settings.database_path)):
        database.initialize()
    with database.transaction() as connection:
        connection.execute("INSERT INTO settings(key,value) VALUES ('web_port',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(web_port),))
    backups = BackupManager(database, settings.data_root / "profiles", settings.data_root / "backups")
    with step('manager.recover_interrupted_backups'):
        backups.recover_interrupted()
    profiles = ProfileManager(database, settings.data_root / "profiles", auto_port_start=settings.auto_port_start, auto_port_end=settings.auto_port_end, runtime=settings.runtime)
    chrome = ChromeService(database, settings, web_port)
    processes = ProcessManager(database, settings.shutdown_timeout, settings)
    processes.recover_interrupted_states()
    instance_status = StatusSynchronizer(database, profiles, settings)
    cdp_monitor = CdpMonitor(database)

    @asynccontextmanager
    async def lifespan(app):
        with operation('manager.start_monitors'):
            with step('manager.start_instance_status'):
                instance_status.start()
            with step('manager.start_cdp_monitor'):
                cdp_monitor.start()
        try:
            yield
        finally:
            with operation('manager.stop_monitors'):
                with step('manager.stop_cdp_monitor'):
                    cdp_monitor.stop()
                with step('manager.stop_instance_status'):
                    instance_status.stop()

    app = FastAPI(title="Chrome Manager", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def log_request_errors(request: Request, call_next):
        request_id = uuid4().hex
        # Do not persist form bodies, query strings, cookies, or authorization headers.
        with operation('http.request', level=logging.DEBUG, request_id=request_id,
                       method=request.method, path=request.url.path,
                       client=request.client.host if request.client else None) as result:
            try:
                unsafe = request.method not in {'GET', 'HEAD', 'OPTIONS'}
                origin = request.headers.get('origin')
                referer = request.headers.get('referer')
                cross_site = request.headers.get('sec-fetch-site') == 'cross-site'
                if unsafe and (cross_site or (origin is not None and not same_origin(origin, str(request.url)))
                               or (origin is None and referer is not None and not same_origin(referer, str(request.url)))):
                    response = JSONResponse({'detail': '拒绝来自其他网站的管理操作，请从本机管理页面操作'}, status_code=403)
                else:
                    response = await call_next(request)
            except Exception as exc:
                event('http.request.error', level=logging.ERROR, exc_info=True,
                      error_type=type(exc).__name__, error=str(exc))
                response = JSONResponse({'detail': '管理服务发生异常，请查看 logs/error.log 后重试',
                                         'request_id': request_id}, status_code=500)
            result['status_code'] = response.status_code
            response.headers['X-Request-ID'] = request_id
            return response

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    mount_desktop(app, settings, profiles)

    @app.get("/version")
    def version() -> JSONResponse:
        return JSONResponse(deployment_version, headers={"Cache-Control": "no-store"})

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> RedirectResponse:
        """The desktop console combines the former dashboard and instance views."""
        return RedirectResponse(url="/", status_code=303)

    def lan_warning(port: int | None = None) -> str | None:
        if not settings.relay_enabled or not runtime_details(database):
            return None
        try:
            status = json.loads((settings.data_root / 'data/cdp-lan-status.json').read_text(encoding='utf-8'))
            if time.time() - status['checked_at'] > 30:
                return 'CDP 局域网转发状态已过期，请检查后台同步任务'
            if status.get('error'):
                return 'CDP 局域网转发异常，请查看 logs/cdp-lan.log'
            if port is not None:
                if not status.get('listeners', {}).get(str(port), False):
                    return status.get('errors', {}).get(str(port), 'CDP 公开端口尚未就绪')
            else:
                expected = [p.cdp_port for p in profiles.list() if p.status == 'running']
                if status.get('errors') or any(not status.get('listeners', {}).get(str(p), False) for p in expected):
                    return 'CDP 局域网监听尚未就绪'
        except (OSError, ValueError, KeyError, TypeError):
            return '无法获取 CDP 局域网转发状态'
        return None

    @app.get("/api/status")
    def current_status() -> JSONResponse:
        snapshot = instance_status.snapshot()
        return JSONResponse({**snapshot, "instances": {
            str(profile.id): {"status": profile.status} for profile in profiles.list()
        }, "cdp": cdp_monitor.states(), "runtime": runtime_details(database),
            "cdp_warning": cdp_monitor.error() or lan_warning()}, headers={"Cache-Control": "no-store"})

    @app.get('/api/cdp/{port}/health')
    def cdp_health(port: int) -> JSONResponse:
        profile = next((p for p in profiles.list() if p.cdp_port == port), None)
        if profile is None:
            raise HTTPException(status_code=404, detail='未找到受管 CDP 端口')
        runtime = runtime_details(database).get(profile.id)
        state = cdp_monitor.states().get(profile.id)
        result = {'port': port, 'profile_id': profile.id, 'ready': False,
                  'state': profile.status, 'failed_pages': []}
        if profile.status != 'running' or runtime is None:
            return JSONResponse(result, status_code=503, headers={'Cache-Control': 'no-store'})
        if state is None or state['pid'] != runtime['pid']:
            result['state'] = 'checking'
        else:
            result.update(state)
            relay_error = lan_warning(port)
            result['ready'] = state['state'] == 'ok' and cdp_monitor.error() is None and relay_error is None
            if relay_error:
                result['relay_error'] = relay_error
        if cdp_monitor.error():
            result['monitor_error'] = cdp_monitor.error()
        return JSONResponse(result, status_code=200 if result['ready'] else 503,
                            headers={'Cache-Control': 'no-store'})

    @app.get("/health")
    def health() -> JSONResponse:
        snapshot = instance_status.snapshot()
        state_healthy = not snapshot["warning"] and time.time() - snapshot["checked_at"] <= 30
        cdp_states = cdp_monitor.states()
        running = runtime_details(database)
        failed = [profile_id for profile_id, runtime in running.items()
                  if (state := cdp_states.get(profile_id)) and state["pid"] == runtime["pid"] and state["state"] == "failed"]
        checking = [profile_id for profile_id, runtime in running.items()
                    if (state := cdp_states.get(profile_id)) is None or state["pid"] != runtime["pid"]]
        status = "degraded" if not state_healthy or failed or cdp_monitor.error() or lan_warning() else "checking" if checking else "ok"
        return JSONResponse({"status": status, "cdp_failed": failed, "cdp_checking": checking,
                             "cdp_warning": cdp_monitor.error() or lan_warning(), "state_warning": snapshot["warning"]},
                            status_code=503 if status == "degraded" else 200)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        sort_by = request.query_params.get("sort", "port")
        if sort_by not in {"port", "project", "platform", "status"}:
            sort_by = "port"
        profile_list = sort_profiles(profiles.list(), sort_by)
        active_runtime = runtime_details(database)
        cdp_states = cdp_monitor.states()
        desktop_profiles = [
            {
                "id": profile.id,
                "name": profile.name,
                "display_name": display_profile_name(profile),
                "status": profile.status,
                "port": profile.cdp_port,
                "description": profile.description or "暂无描述",
                "proxy": profile.proxy_url or "直连",
                **active_runtime.get(profile.id, {"pid": None, "started_at": None}),
                "cdp_health": (
                    cdp_states[profile.id]["state"]
                    if profile.id in cdp_states and cdp_states[profile.id]["pid"] == active_runtime.get(profile.id, {}).get("pid")
                    else "checking" if profile.status == "running" else "stopped"
                ),
                "cdp_reason": (
                    cdp_states[profile.id].get("reason", "")
                    if profile.id in cdp_states and cdp_states[profile.id]["pid"] == active_runtime.get(profile.id, {}).get("pid")
                    else ""
                ),
                "backups": backups.list(profile) if profile.status == "stopped" else [],
            }
            for profile in profile_list
        ]
        selected_name = request.query_params.get("selected", "")
        selected_profile = next((profile for profile in desktop_profiles if profile["name"] == selected_name), None)
        selected_profile_id = selected_profile["id"] if selected_profile else (desktop_profiles[0]["id"] if desktop_profiles else None)
        try:
            suggested_port = profiles.suggested_port()
        except ProfileError:
            suggested_port = None
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "profiles": profile_list, "desktop_profiles": desktop_profiles, "sort_by": sort_by,
                "suggested_port": suggested_port, "detected_proxy": detected_local_proxy(),
                "selected_profile_id": selected_profile_id, "selected_profile": selected_profile, "error": None,
            },
        )

    @app.get("/profiles")
    def profiles_index() -> RedirectResponse:
        """Keep direct navigation to the profile collection on the management page."""
        return home_redirect()

    @app.post("/profiles")
    def create_profile(
        project_name: str = Form(""), platform: str = Form(""),
        account_name: str = Form(""), description: str = Form(""), tags: str = Form(""), port: str = Form(""),
        default_url: str = Form(""), proxy_url: str = Form(""), use_detected_proxy: bool = Form(False),
    ) -> RedirectResponse:
        try:
            generated_name = f"profile-{port or profiles.suggested_port()}"
            profile = profiles.create(
                generated_name, project_name=project_name or None, platform=platform or None, account_name=account_name or None,
                description=description or None, tags=tags or None, cdp_port=int(port) if port else None,
                default_url=default_url or None,
                proxy_url=proxy_url or (detected_local_proxy() if use_detected_proxy else None),
            )
            chrome.start(profile)
        except (ProfileError, ChromeStartError, ValueError) as exc:
            return error_redirect(str(exc))
        return home_redirect(profile.name)

    @app.get("/profiles/{name}", response_class=HTMLResponse)
    def profile_detail(request: Request, name: str) -> HTMLResponse:
        try:
            profile = profiles.show(name)
        except ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(request, "profile.html", {"profile": profile})

    @app.get("/profiles/{name}/edit", response_class=HTMLResponse)
    def edit_profile_page(request: Request, name: str) -> HTMLResponse:
        try:
            profile = profiles.show(name)
        except ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if profile.status != 'stopped':
            return error_redirect('请先停止实例后再编辑', name)
        return templates.TemplateResponse(
            request, "edit_profile.html", {"profile": profile}
        )

    @app.post("/profiles/{name}/edit")
    def update_profile(
        name: str, project_name: str = Form(""), platform: str = Form(""), account_name: str = Form(""),
        description: str = Form(""), default_url: str = Form(""), proxy_url: str = Form(""),
        proxy_enabled: bool = Form(False),
    ) -> RedirectResponse:
        try:
            if proxy_enabled and not proxy_url.strip():
                raise ProfileError('启用代理时必须填写代理地址')
            profiles.update(
                name, project_name=project_name or None, platform=platform or None, account_name=account_name or None,
                description=description or None, default_url=default_url or None,
                proxy_url=proxy_url if proxy_enabled else None,
            )
        except ProfileError as exc:
            return error_redirect(str(exc), name)
        return home_redirect(name)

    @app.post("/profiles/{name}/delete")
    def delete_profile(name: str, selected: str = "") -> RedirectResponse:
        try:
            profiles.delete(name)
        except ProfileError as exc:
            return error_redirect(str(exc), selected or name)
        return home_redirect(selected)

    @app.post("/profiles/{name}/backup")
    def backup_profile(name: str) -> RedirectResponse:
        try:
            backup_id = backups.backup(profiles.show(name))
        except (ProfileError, BackupError) as exc:
            return error_redirect(str(exc), name)
        return home_redirect(name, notice=f"备份完成：{backup_id}")

    @app.post("/profiles/{name}/restore")
    def restore_profile(name: str, backup_id: str = Form(""), confirmed: bool = Form(False)) -> RedirectResponse:
        try:
            if not confirmed:
                raise BackupError("请确认恢复操作后重试")
            previous_id = backups.restore(profiles.show(name), backup_id)
        except (ProfileError, BackupError) as exc:
            return error_redirect(str(exc), name)
        return home_redirect(name, notice=f"恢复完成；恢复前数据已备份为 {previous_id}")

    @app.post("/profiles/{name}/stop")
    def stop_profile(name: str, selected: str = "") -> RedirectResponse:
        try:
            profile = profiles.show(name)
            processes.stop_profile(profile)
        except (ProfileError, ProcessStopError) as exc:
            return error_redirect(str(exc), selected or name)
        return home_redirect(selected or name)

    @app.post("/profiles/{name}/start")
    def start_profile(name: str, selected: str = "") -> RedirectResponse:
        try:
            chrome.start(profiles.show(name))
        except (ProfileError, ChromeStartError) as exc:
            return error_redirect(str(exc), selected or name)
        return home_redirect(selected or name)

    @app.post("/profiles/{name}/restart")
    def restart_profile(name: str, selected: str = "") -> RedirectResponse:
        try:
            with operation('browser.restart', profile_name=name) as result:
                profile = profiles.show(name)
                result.update(profile_id=profile.id, port=profile.cdp_port)
                if profile.status == "running":
                    processes.stop_profile(profile)
                    profile = profiles.show(name)
                result['browser_pid'] = chrome.start(profile)
        except (ProfileError, ChromeStartError, ProcessStopError) as exc:
            return error_redirect(str(exc), selected or name)
        return home_redirect(selected or name)

    @app.post("/profiles/{name}/focus")
    def focus_profile(name: str, selected: str = "", view: bool = False) -> RedirectResponse:
        try:
            profile = profiles.show(name)
            with operation('browser.focus', profile_id=profile.id, profile_name=profile.name):
                processes.focus_profile(profile)
        except (ProfileError, ProcessStopError) as exc:
            return error_redirect(str(exc), selected or name)
        if view:
            return RedirectResponse(url=f"/profiles/{quote(name, safe='')}/browser", status_code=303)
        return home_redirect(selected or name, notice="浏览器窗口已激活")

    return app
