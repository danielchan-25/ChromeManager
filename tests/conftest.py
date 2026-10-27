import logging
import pytest


@pytest.fixture(autouse=True)
def close_application_log_handlers():
    """Each test owns its temporary logs and capture stream; don't leak either."""
    yield
    logger = logging.getLogger('chrome_manager')
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
