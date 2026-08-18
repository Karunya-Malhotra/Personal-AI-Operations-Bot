"""Shared fixtures.

Note what is *not* here: no global settings, no shared engine, no autouse
database fixture. Tests construct what they need. That is a direct consequence
of bootstrap having no module-level globals -- it is what makes it cheap.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.config.settings import Settings
from app.core.environment import Environment


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove APP_* / DATABASE_* from the environment so Settings sees only defaults.

    Without this, running tests on a machine with a real `.env` would silently
    test that machine's configuration instead of the code.
    """
    for key in list(os.environ):
        if key.upper().startswith(("APP_", "DATABASE_", "DB_", "LOG_", "API_")):
            monkeypatch.delenv(key, raising=False)
    # model_config is a TypedDict, not an object: setitem, not setattr. This
    # stops Settings() from reading a developer's real .env during tests.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    yield


@pytest.fixture
def dev_settings(clean_env: None) -> Settings:
    return Settings(
        app_env=Environment.DEV,
        database_url="postgresql+asyncpg://u:p@localhost:5432/aiops_test",  # type: ignore[arg-type]
    )
