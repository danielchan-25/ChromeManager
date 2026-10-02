"""Foreground entry point for the ChromeManager web interface."""

from __future__ import annotations

import logging

import typer

from chrome_manager.config import load_settings
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
        config = uvicorn.Config(web_app, host=settings.bind_address, port=port)
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
