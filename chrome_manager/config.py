"""Program-level configuration stored in TOML, never profile data."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DATA_ROOT = Path("D:/ChromeManager")
ENV_DATA_ROOT = "CHROME_MANAGER_DATA_ROOT"


@dataclass(frozen=True, slots=True)
class Settings:
    data_root: Path
    chrome_path: str = ""
    bind_address: str = "127.0.0.1"
    auto_port_start: int = 9500
    auto_port_end: int = 9999
    startup_timeout: int = 15
    shutdown_timeout: int = 10
    log_max_size_mb: int = 10
    log_backup_count: int = 10

    @property
    def config_path(self) -> Path:
        return self.data_root / "config" / "settings.toml"

    @property
    def database_path(self) -> Path:
        return self.data_root / "data" / "chrome_manager.db"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        from chrome_manager.exceptions import ConfigurationError

        raise ConfigurationError(f"Unable to read settings file: {path}") from exc


def load_settings(data_root: Path | None = None) -> Settings:
    """Load built-in defaults, then TOML, then the environment override."""
    env_root = os.environ.get(ENV_DATA_ROOT)
    root = Path(env_root) if env_root else (data_root or DEFAULT_DATA_ROOT)
    root = root.expanduser().resolve()
    raw = _read_toml(root / "config" / "settings.toml")

    data = raw.get("data", {})
    browser = raw.get("browser", {})
    cdp = raw.get("cdp", {})
    process = raw.get("process", {})
    logging = raw.get("logging", {})
    configured_root = data.get("root")
    if configured_root and not env_root and data_root is None:
        root = Path(configured_root).expanduser().resolve()

    return Settings(
        data_root=root,
        chrome_path=str(browser.get("chrome_path", "")),
        bind_address=str(cdp.get("bind_address", "127.0.0.1")),
        auto_port_start=int(cdp.get("auto_port_start", 9500)),
        auto_port_end=int(cdp.get("auto_port_end", 9999)),
        startup_timeout=int(cdp.get("startup_timeout", 15)),
        shutdown_timeout=int(process.get("shutdown_timeout", 10)),
        log_max_size_mb=int(logging.get("max_size_mb", 10)),
        log_backup_count=int(logging.get("backup_count", 10)),
    )


def ensure_data_directories(settings: Settings) -> None:
    """Create the fixed program directories without creating profile directories."""
    for directory in (
        settings.data_root / "data",
        settings.data_root / "profiles",
        settings.data_root / "backups",
        settings.data_root / "logs" / "profiles",
        settings.data_root / "config",
    ):
        directory.mkdir(parents=True, exist_ok=True)


def write_default_settings(settings: Settings) -> Path:
    """Write a human-editable initial file only when it does not already exist."""
    ensure_data_directories(settings)
    path = settings.config_path
    if path.exists():
        return path
    path.write_text(
        "[data]\n"
        f'root = "{settings.data_root.as_posix()}"\n\n'
        "[browser]\nchrome_path = \"\"\n\n"
        "[cdp]\nbind_address = \"127.0.0.1\"\nauto_port_start = 9500\nauto_port_end = 9999\nstartup_timeout = 15\n\n"
        "[process]\nshutdown_timeout = 10\n\n"
        "[logging]\nmax_size_mb = 10\nbackup_count = 10\n",
        encoding="utf-8",
    )
    return path
