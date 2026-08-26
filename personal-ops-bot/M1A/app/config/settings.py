"""Typed configuration, loaded once at startup.

Design decisions worth knowing about:

1. `Settings` is a Pydantic model, not a dict and not `os.getenv` calls scattered
   through the code. A typo in an env var name becomes a startup error with a
   field name attached, instead of a `None` that surfaces three layers away.

2. There is deliberately **no module-level `settings = Settings()`**. That
   pattern is convenient and it is why so many codebases end up untestable:
   every import becomes a hidden dependency on the environment, and overriding
   config in a test means monkeypatching a module global. Instead `bootstrap.py`
   constructs one instance and passes it down. Nothing outside bootstrap imports
   this module -- an import-linter contract enforces that for `app.db`.

3. Every secret is `SecretStr`. `repr()` and `str()` of a SecretStr return
   `**********`, so a stray log line, an exception message, or a `/debug`
   endpoint cannot leak it. Reading the real value requires
   `.get_secret_value()`, which is greppable -- and at M1C the CredentialBroker
   will be the only thing allowed to call it.

M1A has almost no configuration. That is the point: the shape is established
now, so M1B adds `ANTHROPIC_API_KEY` as one field rather than as a pattern.
"""

from __future__ import annotations

from functools import cached_property

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.environment import Environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # an unrecognised key in .env is a typo, not a feature
        frozen=True,  # configuration does not change while the process runs
    )

    # --- identity of this deployment -------------------------------------
    app_env: Environment = Field(
        default=Environment.DEV,
        description="Which deployment this process is. Checked against the database stamp.",
    )

    # --- database ---------------------------------------------------------
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://aiops:aiops@localhost:5432/aiops_dev"),
        description="Async SQLAlchemy DSN. Secret because it contains a password.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_pool_max_overflow: int = Field(default=5, ge=0, le=50)
    db_connect_timeout_s: float = Field(default=5.0, gt=0)

    # --- observability ----------------------------------------------------
    log_level: str = Field(default="INFO")
    log_json: bool | None = Field(
        default=None,
        description="JSON logs. Defaults to True in prod, False in dev (human-readable).",
    )

    # --- llm ---------------------------------------------------------------
    llm_provider: str = Field(
        default="anthropic",
        description="Which adapter to construct. See app/providers/llm/factory.py.",
    )
    llm_model: str = Field(
        default="claude-opus-5",
        description=(
            "Model id, passed straight through to the provider. The default is an "
            "Anthropic model; switching LLM_PROVIDER to gemini requires setting this "
            "too, since the id vocabularies are unrelated."
        ),
    )
    llm_max_tokens: int = Field(default=4096, ge=1, le=200_000)
    llm_timeout_s: float = Field(default=30.0, gt=0)

    anthropic_api_key: SecretStr | None = Field(
        default=None, description="Required when llm_provider=anthropic."
    )
    gemini_api_key: SecretStr | None = Field(
        default=None, description="Required when llm_provider=gemini."
    )

    # --- api --------------------------------------------------------------
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {v!r}")
        return upper

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        """Fail at startup on a typo rather than at the first model call.

        The set is duplicated from the factory rather than imported, because
        `config` must not depend on `providers` -- an import-linter contract
        enforces that direction. A unit test asserts the two stay in agreement,
        which is the same enum-vs-CHECK-constraint pattern v0.3.1 §E.2 uses.
        """
        allowed = {"anthropic", "gemini", "fake"}
        if v not in allowed:
            raise ValueError(f"llm_provider must be one of {sorted(allowed)}, got {v!r}")
        return v

    @property
    def is_prod(self) -> bool:
        return self.app_env is Environment.PROD

    @property
    def use_json_logs(self) -> bool:
        return self.is_prod if self.log_json is None else self.log_json

    @cached_property
    def database_host(self) -> str:
        """Host portion of the DSN, safe to log and to show in error messages.

        Used by the environment guard so its failure message can say *which*
        database it refused to talk to, without printing the password.
        """
        dsn = PostgresDsn(self.database_url.get_secret_value())
        return dsn.hosts()[0].get("host") or "unknown"
