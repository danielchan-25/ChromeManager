import logging
from pathlib import Path

from chrome_manager.utils.logger import configure_logging
from chrome_manager.utils.logger import RedactingFormatter


def test_exception_and_authorization_are_redacted():
    record = logging.LogRecord('test', logging.ERROR, '', 0,
        'Authorization: Bearer secret-value http://user:private@localhost token=hidden', (), None)
    output = RedactingFormatter('%(message)s').format(record)
    assert 'secret-value' not in output
    assert 'private' not in output
    assert 'hidden' not in output


def test_logger_masks_sensitive_values(tmp_path: Path) -> None:
    logger = configure_logging(tmp_path, max_size_mb=1, backup_count=1)
    logger.error("password=secret token: abc Cookie=xyz")
    for handler in logger.handlers:
        handler.flush()
    output = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "secret" not in output
    assert "abc" not in output
    assert "xyz" not in output
    assert "password=***" in output
    logging.getLogger("chrome_manager").handlers.clear()
