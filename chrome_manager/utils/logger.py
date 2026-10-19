"""Rotating file logging with conservative sensitive value masking."""

from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

from chrome_manager.utils.audit import context

_SECRET_PATTERN = re.compile(r"(?i)(password|token|authorization|cookie)\s*([=:])\s*([^\s,;]+)")


def redact_text(output: str) -> str:
    output = re.sub(r'(?i)(authorization\s*[=:]\s*)(?:bearer|basic)\s+\S+', r'\1***', output)
    # URLs may carry login credentials or signed query parameters.
    def clean_url(match):
        try:
            parsed = urlsplit(match.group(0))
            host = parsed.netloc.rsplit('@', 1)[-1]
            path = re.sub(r'(/open-apis/bot/v2/hook/)[^/]+', r'\1***', parsed.path)
            return urlunsplit((parsed.scheme, host, path, '', ''))
        except ValueError:
            return '[invalid URL]'
    output = re.sub(r'(?:https?|wss?)://[^\s\"\'<>]+', clean_url, output)
    return _SECRET_PATTERN.sub(r'\1\2***', output)


def redact_value(value, key: str = ''):
    if re.search(r'(?i)password|token|authorization|cookie|secret|webhook|^sign$', key):
        return '***'
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return redact_text(value) if isinstance(value, str) else value


class LogContextFilter(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        record.audit_fields = {**context(), **getattr(record, 'audit_fields', {})}
        for key in ('request_id', 'operation_id', 'profile_id', 'port'):
            setattr(record, key, record.audit_fields.get(key, '-'))
        return True


class DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds'),
            'level': record.levelname, 'logger': record.name,
            'process_id': record.process, 'thread': record.threadName,
            'run_id': record.run_id, 'event': record.getMessage(),
            **redact_value(record.audit_fields),
        }
        payload['event'] = redact_text(payload['event'])
        if record.exc_info:
            payload['traceback'] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        return redact_text(output)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = _SECRET_PATTERN.sub(r"\1\2***", message)
        record.args = ()
        return True


def configure_logging(log_directory: Path, max_size_mb: int, backup_count: int) -> logging.Logger:
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("chrome_manager")
    logger.setLevel(logging.DEBUG)
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.propagate = False
    run_id = uuid4().hex
    context_filter = LogContextFilter(run_id)
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s [run=%(run_id)s request=%(request_id)s "
        "operation=%(operation_id)s profile=%(profile_id)s port=%(port)s] %(message)s"
    )
    data_handler = RotatingFileHandler(
        log_directory / "chrome_manager.log", maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count, encoding="utf-8"
    )
    error_handler = RotatingFileHandler(
        log_directory / "error.log", maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    data_handler.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    for handler in (data_handler, error_handler, console):
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    diagnostic = RotatingFileHandler(
        log_directory / 'diagnostics.jsonl', maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count, encoding='utf-8',
    )
    diagnostic.addFilter(context_filter)
    diagnostic.setFormatter(DiagnosticFormatter())
    diagnostic.setLevel(logging.DEBUG)
    logger.addHandler(diagnostic)
    logger.info('日志系统已初始化：run_id=%s，详细日志=%s，单文件上限=%sMB，保留副本=%s',
                run_id, log_directory / 'diagnostics.jsonl', max_size_mb, backup_count)
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
