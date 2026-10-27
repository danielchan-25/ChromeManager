"""Correlated lifecycle and diagnostic events without browser content."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4
from functools import wraps

_context: ContextVar[dict] = ContextVar('chrome_manager_log_context', default={})


def context() -> dict:
    return _context.get().copy()


def event(name: str, *, level: int = logging.DEBUG, exc_info: bool = False, **fields) -> None:
    details = {**context(), **fields}
    logging.getLogger('chrome_manager.audit').log(
        level, name, extra={'audit_fields': details}, exc_info=exc_info,
    )


def profile_operation(name: str):
    """Trace metadata and backup actions without recording submitted account data."""
    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            profile = args[0] if args else kwargs.get('profile', kwargs.get('name'))
            fields = {'profile_name': getattr(profile, 'name', profile)}
            if hasattr(profile, 'id'):
                fields.update(profile_id=profile.id, port=profile.cdp_port)
            with operation(name, **fields) as outcome:
                result = function(self, *args, **kwargs)
                if hasattr(result, 'id'):
                    outcome.update(profile_id=result.id, port=result.cdp_port)
                elif name.startswith('backup.') and isinstance(result, str):
                    outcome['backup_id'] = result
                return result
        return wrapped
    return decorate


@contextmanager
def operation(name: str, *, level: int = logging.INFO, **fields):
    """Give each operation an ID while retaining its parent/request context."""
    previous = context()
    token = _context.set({**previous, **fields, 'operation': name,
                          'parent_operation_id': previous.get('operation_id'),
                          'operation_id': uuid4().hex})
    try:
        with step(name, level=level) as result:
            yield result
    finally:
        _context.reset(token)


@contextmanager
def step(name: str, *, level: int = logging.DEBUG, failure_level: int = logging.ERROR, **fields):
    """Record both ends of a step, including failures and their traceback."""
    started = time.perf_counter()
    details = dict(fields)
    event(name + '.begin', level=level, **details)
    try:
        yield details
    except Exception as exc:
        event(name + '.failed', level=failure_level, exc_info=True,
              duration_ms=round((time.perf_counter() - started) * 1000, 2),
              error_type=type(exc).__name__, error=str(exc), **details)
        raise
    else:
        event(name + '.completed', level=level,
              duration_ms=round((time.perf_counter() - started) * 1000, 2), **details)
