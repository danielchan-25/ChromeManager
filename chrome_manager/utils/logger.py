"""Rotating file logging with conservative sensitive value masking."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERN = re.compile(r"(?i)(password|token|authorization|cookie)\s*([=:])\s*([^\s,;]+)")


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        output = re.sub(r'(?i)(authorization\s*[=:]\s*)(?:bearer|basic)\s+\S+', r'\1***', output)
        output = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1***@', output)
        return _SECRET_PATTERN.sub(r'\1\2***', output)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = _SECRET_PATTERN.sub(r"\1\2***", message)
        record.args = ()
        return True


def configure_logging(log_directory: Path, max_size_mb: int, backup_count: int) -> logging.Logger:
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("chrome_manager")
    logger.setLevel(logging.INFO)
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.propagate = False
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    data_handler = RotatingFileHandler(
        log_directory / "chrome_manager.log", maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count, encoding="utf-8"
    )
    error_handler = RotatingFileHandler(
        log_directory / "error.log", maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    for handler in (data_handler, error_handler, logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def capture_web_console(log_directory: Path, max_size_mb: int, backup_count: int) -> None:
    """Persist Uvicorn's console output so it is available in the local web UI."""
    log_directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_directory / "console.log", maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
