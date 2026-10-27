"""Foreground entry point for the ChromeManager web interface."""

from __future__ import annotations

import logging
import asyncio
import json
import os
from pathlib import Path
import signal

import typer

from chrome_manager.config import load_settings, ensure_data_directories, write_default_settings
from chrome_manager.core.service_lock import service_lock, ServiceAlreadyRunning
from chrome_manager.exceptions import ChromeManagerError, ExitCode
from chrome_manager.utils.logger import capture_web_console

app = typer.Typer(no_args_is_help=True, help="启动 ChromeManager 页面。")


@app.callback()
def _main() -> None:
    """Keep `web` as an explicit subcommand for existing startup scripts."""


@app.command("web")
def web(
    port: int = typer.Option(8765, min=1, max=65535),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅校验管理服务，不启动 HTTP 服务或 Chrome。"),
) -> None:
    """在当前终端启动管理页面，按 Ctrl+C 停止。"""
    try:
        settings = load_settings()
        with service_lock(settings.data_root / 'data'):
            _run_web(port, dry_run)
    except ServiceAlreadyRunning as exc:
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.ALREADY_RUNNING) from exc
    except ChromeManagerError as exc:
        typer.echo(f"启动失败：{exc}", err=True)
        raise typer.Exit(ExitCode.INVALID_CONFIGURATION) from exc


def _run_web(port: int, dry_run: bool) -> None:
    import uvicorn

    from chrome_manager.web import create_app

    try:
        settings = load_settings()
        web_app = create_app(web_port=port)
    except ChromeManagerError as exc:
        typer.echo(f"启动失败：{exc}", err=True)
        raise typer.Exit(ExitCode.INVALID_CONFIGURATION) from exc

    local_url = f"http://127.0.0.1:{port}"
    if dry_run:
        typer.echo(f"检查通过：ChromeManager 页面可在 {local_url} 启动")
        return
    typer.echo(f"ChromeManager 页面：{local_url}")
    if settings.bind_address in {"0.0.0.0", "::"}:
        typer.echo(f"局域网访问：http://<本机局域网IP>:{port}（bind_address={settings.bind_address}）")
    typer.echo("按 Ctrl+C 停止服务。")
    logger = logging.getLogger("chrome_manager.cli")
    try:
        config = uvicorn.Config(web_app, host=settings.bind_address, port=port, timeout_graceful_shutdown=15)
        capture_web_console(settings.data_root / "logs", settings.log_max_size_mb, settings.log_backup_count)
        logger.info("管理服务启动：bind=%s port=%s", settings.bind_address, port)
        server = uvicorn.Server(config)
        server.run()
        if not server.started:
            logger.error("管理服务未能启动：bind=%s port=%s", settings.bind_address, port)
            raise typer.Exit(ExitCode.GENERAL_ERROR)
        logger.info("管理服务已停止：bind=%s port=%s", settings.bind_address, port)
    except KeyboardInterrupt:
        logger.info("管理服务收到 Ctrl+C，已停止")
    except OSError as exc:
        logger.exception("管理服务启动失败：bind=%s port=%s", settings.bind_address, port)
        raise typer.Exit(ExitCode.GENERAL_ERROR) from exc
    except typer.Exit:
        raise
    except SystemExit as exc:
        if exc.code:
            logger.error('管理服务异常退出，退出码：%s', exc.code)
        raise
    except Exception as exc:
        logger.exception('管理服务发生未处理异常')
        raise typer.Exit(ExitCode.GENERAL_ERROR) from exc


@app.command('init')
def initialize(ports: str = typer.Option('', help='可选逗号分隔端口，例如 9600,9601,9610')):
    from chrome_manager.db.database import Database
    from chrome_manager.core.profile_manager import ProfileManager
    settings = load_settings()
    ensure_data_directories(settings)
    write_default_settings(settings)
    db = Database(settings.database_path)
    db.initialize()
    manager = ProfileManager(db, settings.data_root / 'profiles')
    for port in (int(p.strip()) for p in ports.split(',') if p.strip()):
        name = f'profile-{port}'
        if not any(p.name == name for p in manager.list()):
            manager.create(name, cdp_port=port)
    typer.echo(f'Initialized {settings.data_root}')


@app.command('relay')
def relay():
    from chrome_manager.core.relay import CdpRelay
    from chrome_manager.db.database import Database
    from chrome_manager.utils.logger import configure_logging
    settings = load_settings()
    ensure_data_directories(settings)
    db = Database(settings.database_path)
    db.initialize()
    configure_logging(settings.data_root / 'logs/relay', settings.log_max_size_mb, settings.log_backup_count)
    with service_lock(settings.data_root / 'data/relay'):
        async def run():
            daemon = CdpRelay(db, settings)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, daemon.stop_event.set)
            await daemon.run()
        asyncio.run(run())


@app.command('browser-run')
def browser_run(profile_id: int):
    """systemd 实例入口；exec 使实际 Chrome 保持 MainPID，禁止通过此入口管理其他用户浏览器。"""
    from chrome_manager.core.chrome_service import ChromeService
    from chrome_manager.core.cdp_ports import browser_port
    from chrome_manager.core.linux_runtime import matches_profile
    from chrome_manager.core.profile_manager import ProfileManager
    from chrome_manager.db.database import Database
    import psutil
    settings = load_settings()
    ensure_data_directories(settings)
    db = Database(settings.database_path)
    db.initialize()
    manager = ProfileManager(db, settings.data_root / 'profiles')
    profile = next((p for p in manager.list() if p.id == profile_id), None)
    if profile is None:
        raise typer.BadParameter('实例不存在或已删除')
    port = browser_port(db, profile, True)
    for process in psutil.process_iter(['name']):
        if matches_profile(process, profile.user_data_dir, port):
            raise typer.BadParameter('该实例已有 Chrome 运行，拒绝重复启动')
    service = ChromeService(db, settings)
    if not settings.headless and not os.environ.get('DISPLAY'):
        raise typer.BadParameter('browser-run 有界面模式必须配置 DISPLAY')
    arguments = service.arguments(profile, port)
    log, native = service.prepare_logs(profile)
    with log.open('ab', buffering=0) as output:
        output.write(f'\n=== systemd browser-run profile={profile_id} backend={port} ===\n'.encode())
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
        os.execvpe(arguments[0], arguments, {**os.environ, 'CHROME_LOG_FILE': str(native)})


def profile_context(name):
    from chrome_manager.core.profile_manager import ProfileManager
    from chrome_manager.db.database import Database
    settings = load_settings()
    db = Database(settings.database_path)
    manager = ProfileManager(db, settings.data_root / 'profiles')
    return settings, db, manager, manager.show(name)


@app.command('profile-start')
def profile_start(name: str):
    from chrome_manager.core.chrome_service import ChromeService
    from chrome_manager.core.process_manager import ProcessManager
    settings, db, manager, _ = profile_context(name)
    processes = ProcessManager(db, settings.shutdown_timeout, settings)
    processes.reconcile()
    processes.recover_restarted(manager.list())
    typer.echo(ChromeService(db, settings).start(manager.show(name)))


@app.command('profile-stop')
def profile_stop(name: str):
    from chrome_manager.core.process_manager import ProcessManager
    settings, db, _, profile = profile_context(name)
    ProcessManager(db, settings.shutdown_timeout, settings).stop_profile(profile)
    typer.echo(f'Stopped {name}')


@app.command('profile-autostart')
def profile_autostart(name: str, enabled: bool = typer.Option(True, '--enable/--disable')):
    from chrome_manager.core.cdp_ports import browser_port
    from chrome_manager.core.linux_runtime import systemctl, unit_name
    settings, db, _, profile = profile_context(name)
    if settings.runtime != 'systemd':
        raise typer.BadParameter('开机启动需要 process.runtime=systemd')
    browser_port(db, profile, True)
    systemctl('enable' if enabled else 'disable', unit_name(profile.id))
    typer.echo(f'{name}: autostart={enabled}; enabling does not start it now')


@app.command('import-windows')
def import_windows(source: Path = typer.Argument(..., exists=True, dir_okay=False)):
    from chrome_manager.core.migration import import_windows_metadata
    settings = load_settings()
    ensure_data_directories(settings)
    # Offline operation: the same lock is held by Web. Never rewrite live sessions.
    with service_lock(settings.data_root / 'data'):
        names = import_windows_metadata(source, settings)
    typer.echo(json.dumps({'imported': names, 'credentials_imported': False}, ensure_ascii=False))


@app.command('doctor')
def doctor():
    from chrome_manager.core.chrome_service import ChromeService
    from chrome_manager.db.database import Database
    settings = load_settings()
    try:
        chrome = str(ChromeService(Database(settings.database_path), settings)._find_chrome())
    except Exception as exc:
        chrome = str(exc)
    typer.echo(json.dumps({'data_root': str(settings.data_root), 'chrome': chrome,
                          'runtime': settings.runtime, 'headless': settings.headless,
                          'display': os.environ.get('DISPLAY'), 'relay': settings.relay_enabled}, ensure_ascii=False))


def main() -> None:
    if os.name == 'posix':
        os.umask(0o077)
    app()


if __name__ == "__main__":
    main()
