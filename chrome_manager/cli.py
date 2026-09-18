"""Phase 1 CLI entry point."""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console

from chrome_manager.config import ensure_data_directories, load_settings, write_default_settings
from chrome_manager.core.profile_manager import ProfileError, ProfileManager
from chrome_manager.core.chrome_service import ChromeService, ChromeStartError
from chrome_manager.core.process_manager import ProcessManager, ProcessStopError
from chrome_manager.db.database import Database
from chrome_manager.exceptions import ChromeManagerError, ExitCode
from chrome_manager.utils.logger import capture_web_console, configure_logging

app = typer.Typer(no_args_is_help=True, help="Manage isolated local Chrome environments.")
config_app = typer.Typer(help="View program-level configuration.")
app.add_typer(config_app, name="config")
console = Console()


def initialize() -> tuple[object, Database]:
    settings = load_settings()
    ensure_data_directories(settings)
    write_default_settings(settings)
    logger = configure_logging(settings.data_root / "logs", settings.log_max_size_mb, settings.log_backup_count)
    database = Database(settings.database_path)
    database.initialize()
    logger.info("Chrome Manager infrastructure initialized")
    return settings, database


@app.command()
def init() -> None:
    """Create data directories, settings file, logs, and SQLite schema."""
    try:
        settings, _ = initialize()
        console.print(f"Initialized Chrome Manager at [green]{settings.data_root}[/green]")
    except ChromeManagerError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(ExitCode.INVALID_CONFIGURATION) from exc


@config_app.command("show")
def config_show() -> None:
    """Display the resolved program configuration."""
    try:
        settings = load_settings()
        console.print(f"Data root: {settings.data_root}")
        console.print(f"Settings : {settings.config_path}")
        console.print(f"Database : {settings.database_path}")
        console.print(f"CDP bind : {settings.bind_address}")
    except ChromeManagerError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(ExitCode.INVALID_CONFIGURATION) from exc


@app.command("create")
def create_profile(
    name: str,
    project: Optional[str] = typer.Option(None),
    platform: Optional[str] = typer.Option(None),
    account: Optional[str] = typer.Option(None),
    port: Optional[int] = typer.Option(None),
    description: Optional[str] = typer.Option(None),
    tags: Optional[str] = typer.Option(None),
    default_url: Optional[str] = typer.Option(None, "--url"),
    proxy: Optional[str] = typer.Option(None),
) -> None:
    """Create an isolated Chrome profile."""
    settings, database = initialize()
    try:
        profile = ProfileManager(database, settings.data_root / "profiles").create(
            name, project_name=project, platform=platform, account_name=account, cdp_port=port,
            description=description, tags=tags, default_url=default_url, proxy_url=proxy,
        )
        pid = ChromeService(database, settings).start(profile)
        console.print(f"Created and started [green]{profile.name}[/green] (PID {pid})")
    except (ProfileError, ChromeStartError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(ExitCode.INVALID_CONFIGURATION) from exc


@app.command("list")
def list_profiles() -> None:
    """List active Chrome profiles."""
    settings, database = initialize()
    for profile in ProfileManager(database, settings.data_root / "profiles").list():
        console.print(f"{profile.name}\t{profile.status}\t{profile.cdp_port or 'auto'}")


@app.command("stop")
def stop_profile(name: str) -> None:
    """Stop only the verified Chrome process belonging to one Profile."""
    settings, database = initialize()
    try:
        profile = ProfileManager(database, settings.data_root / "profiles").show(name)
        ProcessManager(database, settings.shutdown_timeout).stop_profile(profile)
        console.print(f"Stopped [green]{name}[/green]")
    except (ProfileError, ProcessStopError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(ExitCode.GENERAL_ERROR) from exc


@app.command("start")
def start_profile(name: str) -> None:
    """Start a stopped Profile in a visible Chrome window."""
    settings, database = initialize()
    try:
        profile = ProfileManager(database, settings.data_root / "profiles").show(name)
        pid = ChromeService(database, settings).start(profile)
        console.print(f"Started [green]{name}[/green] (PID {pid})")
    except (ProfileError, ChromeStartError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(ExitCode.GENERAL_ERROR) from exc


@app.command("web")
def web(
    port: int = typer.Option(8765, min=1, max=65535),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅校验管理服务，不启动 HTTP 服务或 Chrome。"),
) -> None:
    """Run the local-only management console."""
    import uvicorn

    from chrome_manager.web import create_app

    settings, _ = initialize()
    if dry_run:
        create_app()
        console.print(f"[green]Dry run passed:[/green] Chrome Manager 管理服务可在 http://127.0.0.1:{port} 启动")
        return
    capture_web_console(settings.data_root / "logs", settings.log_max_size_mb, settings.log_backup_count)
    console.print(f"Chrome Manager console: http://127.0.0.1:{port}")
    console.print("按 Ctrl+C 可停止管理服务。")
    try:
        # uvicorn.run blocks in the current terminal; do not detach or create a background process.
        uvicorn.run(create_app(), host="127.0.0.1", port=port)
    except KeyboardInterrupt:
        console.print("Chrome Manager 管理服务已停止。")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
