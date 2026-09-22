"""Local-only FastAPI management console."""

from __future__ import annotations

import os
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import psutil

from chrome_manager.config import ensure_data_directories, load_settings, write_default_settings
from chrome_manager.core.chrome_service import ChromeService, ChromeStartError
from chrome_manager.core.profile_manager import ProfileError, ProfileManager
from chrome_manager.core.process_manager import ProcessManager, ProcessStopError
from chrome_manager.db.database import Database
from chrome_manager.utils.logger import configure_logging

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def home_redirect(selected: str = "", error: str = "") -> RedirectResponse:
    """Return to the console while retaining the instance the user was operating."""
    query = []
    if selected:
        query.append(f"selected={quote(selected)}")
    if error:
        query.append(f"error={quote(error)}")
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
            "SELECT profile_id, pid, started_at FROM runtime_sessions "
            "WHERE stopped_at IS NULL ORDER BY id ASC"
        ).fetchall()
    return {
        int(row["profile_id"]): {"pid": row["pid"], "started_at": row["started_at"]}
        for row in rows
    }


def resource_snapshot(database: Database) -> tuple[dict[str, float], dict[int, dict[str, float]]]:
    """Collect a short, local-only resource snapshot for the dashboard."""
    with database.read() as connection:
        rows = connection.execute(
            "SELECT profile_id, pid FROM runtime_sessions WHERE stopped_at IS NULL AND pid IS NOT NULL"
        ).fetchall()
    profile_processes: dict[int, list[psutil.Process]] = {}
    for row in rows:
        try:
            process = psutil.Process(row["pid"])
            profile_processes[row["profile_id"]] = [process, *process.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    watched = [process for processes in profile_processes.values() for process in processes]
    for process in watched:
        try:
            process.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.1)
    per_profile: dict[int, dict[str, float]] = {}
    for profile_id, processes in profile_processes.items():
        cpu = memory = 0.0
        for process in processes:
            try:
                cpu += process.cpu_percent(interval=None)
                memory += process.memory_info().rss / 1024 / 1024
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        per_profile[profile_id] = {"cpu": round(cpu / (psutil.cpu_count() or 1), 1), "memory": round(memory, 1)}
    memory = psutil.virtual_memory()
    system = {"cpu": round(psutil.cpu_percent(interval=None), 1), "memory_used": round(memory.used / 1024 / 1024 / 1024, 1),
              "memory_total": round(memory.total / 1024 / 1024 / 1024, 1), "memory_percent": round(memory.percent, 1)}
    return system, per_profile


class ResourceMonitor:
    """Retains five minutes of local resource samples for the dashboard."""

    def __init__(self, database: Database, profiles: ProfileManager) -> None:
        self.database = database
        self.profiles = profiles
        self.history: deque[dict[str, object]] = deque(maxlen=31)
        self.error: str | None = None
        self.lock = Lock()
        self.stop_event = Event()
        self.thread: Thread | None = None

    def sample(self) -> dict[str, object]:
        ProcessManager(self.database, 10).reconcile()
        profile_list = self.profiles.list()
        system, usage = resource_snapshot(self.database)
        snapshot = {
            "timestamp": int(time.time()),
            "system": system,
            "instances": {
                str(profile.id): {"name": display_profile_name(profile), "status": profile.status, **usage.get(profile.id, {"cpu": 0.0, "memory": 0.0})}
                for profile in profile_list
            },
        }
        with self.lock:
            self.history.append(snapshot)
            self.error = None
        return snapshot

    def samples(self) -> list[dict[str, object]]:
        with self.lock:
            return list(self.history)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.sample()

        def collect() -> None:
            while not self.stop_event.wait(10):
                try:
                    self.sample()
                except Exception:
                    self.error = '资源采集失败，正在重试；显示的是上次采样数据'
                    logging.getLogger('chrome_manager.monitor').exception(self.error)

        self.thread = Thread(target=collect, name="chrome-manager-resource-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)


def create_app(web_port: int = 8765) -> FastAPI:
    settings = load_settings()
    ensure_data_directories(settings)
    configure_logging(settings.data_root / 'logs', settings.log_max_size_mb, settings.log_backup_count)
    write_default_settings(settings)
    database = Database(settings.database_path)
    database.initialize()
    profiles = ProfileManager(database, settings.data_root / "profiles")
    chrome = ChromeService(database, settings, web_port)
    processes = ProcessManager(database, settings.shutdown_timeout)
    resources = ResourceMonitor(database, profiles)

    @asynccontextmanager
    async def lifespan(app):
        resources.start()
        try:
            yield
        finally:
            resources.stop()

    app = FastAPI(title="Chrome Manager", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])

    @app.middleware('http')
    async def protect_local_actions(request: Request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
                return JSONResponse({'detail': '拒绝跨站修改请求'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '拒绝跨站修改请求'}, status_code=403)
        try:
            return await call_next(request)
        except Exception:
            logging.getLogger('chrome_manager.web').exception('请求失败：%s %s', request.method, request.url.path)
            return JSONResponse({'detail': '管理服务发生异常，请查看 logs/error.log 后重试'}, status_code=500)

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> RedirectResponse:
        """The desktop console combines the former dashboard and instance views."""
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/resources")
    def resource_history() -> dict[str, object]:
        if not resources.samples():
            resources.sample()
        return {"samples": resources.samples(), "warning": resources.error}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        sort_by = request.query_params.get("sort", "port")
        if sort_by not in {"port", "project", "platform", "status"}:
            sort_by = "port"
        profile_list = sort_profiles(profiles.list(), sort_by)
        if not resources.samples():
            resources.sample()
        resource_history = resources.samples()
        latest_resources = resource_history[-1]
        active_runtime = runtime_details(database)
        desktop_profiles = [
            {
                "id": profile.id,
                "name": profile.name,
                "display_name": display_profile_name(profile),
                "status": profile.status,
                "port": profile.cdp_port,
                "description": profile.description or "暂无描述",
                "proxy": profile.proxy_url or "直连",
                "cpu": latest_resources["instances"].get(str(profile.id), {}).get("cpu", 0),
                "memory": latest_resources["instances"].get(str(profile.id), {}).get("memory", 0),
                **active_runtime.get(profile.id, {"pid": None, "started_at": None}),
            }
            for profile in profile_list
        ]
        selected_name = request.query_params.get("selected", "")
        selected_profile = next((profile for profile in desktop_profiles if profile["name"] == selected_name), None)
        selected_profile_id = selected_profile["id"] if selected_profile else (desktop_profiles[0]["id"] if desktop_profiles else None)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "profiles": profile_list, "desktop_profiles": desktop_profiles, "sort_by": sort_by,
                "suggested_port": profiles.suggested_port(), "detected_proxy": detected_local_proxy(),
                "system_resources": latest_resources["system"], "resource_history": resource_history,
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

    @app.post("/profiles/{name}/stop")
    def stop_profile(name: str, selected: str = "") -> RedirectResponse:
        try:
            processes.stop_profile(profiles.show(name))
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
            profile = profiles.show(name)
            if profile.status == "running":
                processes.stop_profile(profile)
                profile = profiles.show(name)
            chrome.start(profile)
        except (ProfileError, ChromeStartError, ProcessStopError) as exc:
            return error_redirect(str(exc), selected or name)
        return home_redirect(selected or name)

    @app.post("/profiles/{name}/focus")
    def focus_profile(name: str, selected: str = "") -> RedirectResponse:
        try:
            processes.focus_profile(profiles.show(name))
        except (ProfileError, ProcessStopError) as exc:
            return error_redirect(str(exc), selected or name)
        return home_redirect(selected or name)

    return app
