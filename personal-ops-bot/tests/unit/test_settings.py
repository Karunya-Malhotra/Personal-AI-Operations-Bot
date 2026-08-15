"""Configuration behaves as a boundary, not as a bag of strings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.core.environment import Environment


def test_defaults_to_dev(clean_env: None) -> None:
    assert Settings().app_env is Environment.DEV


def test_unknown_key_is_a_startup_error(clean_env: None) -> None:
    """`extra="forbid"` turns a typo in .env into a loud failure.

    Without this, `DATABSE_URL=...` would be ignored and the app would silently
    connect to the default database.
    """
    with pytest.raises(ValidationError):
        Settings(datbase_url="oops")  # type: ignore[call-arg]


def test_invalid_log_level_rejected(clean_env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="LOUD")


def test_log_level_is_normalised(clean_env: None) -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_database_url_is_not_printable(dev_settings: Settings) -> None:
    """The single most important property of this file.

    A DSN contains a password. If `repr(settings)` printed it, then any
    exception traceback, any debug endpoint, and any `log.info(settings=...)`
    would leak it. SecretStr makes that impossible by default.
    """
    assert "p@localhost" not in repr(dev_settings)
    assert "p@localhost" not in str(dev_settings.database_url)
    assert dev_settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")


def test_database_host_is_safe_to_show(dev_settings: Settings) -> None:
    assert dev_settings.database_host == "localhost"


def test_json_logs_default_by_environment(clean_env: None) -> None:
    assert Settings(app_env=Environment.PROD).use_json_logs is True
    assert Settings(app_env=Environment.DEV).use_json_logs is False
    assert Settings(app_env=Environment.PROD, log_json=False).use_json_logs is False
