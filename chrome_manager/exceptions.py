"""Domain exceptions and stable CLI exit codes."""

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERAL_ERROR = 1
    DATABASE_ERROR = 7
    INVALID_CONFIGURATION = 10


class ChromeManagerError(Exception):
    """Base exception for expected manager errors."""


class ConfigurationError(ChromeManagerError):
    """Raised for an invalid program configuration."""


class DatabaseError(ChromeManagerError):
    """Raised when the SQLite database cannot be initialized or migrated."""
