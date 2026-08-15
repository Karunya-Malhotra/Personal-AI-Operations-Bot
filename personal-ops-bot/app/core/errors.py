"""Error taxonomy for the whole system.

Lives in `core` because every other layer needs to raise these, and `core`
depends on nothing. Errors are *typed* rather than string-matched: at M1C the
agent runtime turns tool failures into structured results the model can react
to, and it can only do that if the failure has a machine-readable class.

The split that matters here is `ConfigurationError` vs everything else.
Configuration errors are *startup* failures — they mean the process must not
run at all. Every other error is a runtime condition the process should survive.
"""

from __future__ import annotations


class AiopsError(Exception):
    """Base for every error this application raises deliberately."""


class ConfigurationError(AiopsError):
    """The process is misconfigured and must not start.

    Raised only during bootstrap, never during request handling. Examples that
    will use this in later milestones:
      - a tool registered without a policy declaration (M1C)
      - a tool declaring a credential it has no grant for (M1C)
      - the database marked `prod` while APP_ENV=dev (M1A, see db.guard)

    These are all "fail loudly at boot" conditions. The alternative -- starting
    anyway and failing later -- turns a five-second obvious error into an
    intermittent production mystery.
    """


class NotFoundError(AiopsError):
    """A requested entity does not exist (or is not visible to this user)."""


class ValidationError(AiopsError):
    """Input failed validation at an application boundary."""


class UpstreamError(AiopsError):
    """A dependency we do not control failed (LLM API, provider, storage)."""


class TimeoutError_(AiopsError):
    """An operation exceeded its declared budget."""


class EnvironmentMismatchError(ConfigurationError):
    """This process's APP_ENV disagrees with the environment the database claims.

    The concrete accident this exists to prevent: a laptop configured for
    development, pointed at the production database, writing test data into a
    real ledger. See app/db/guard.py.
    """

    def __init__(self, expected: str, found: str, host: str) -> None:
        self.expected = expected
        self.found = found
        self.host = host
        super().__init__(
            f"APP_ENV is {expected!r} but the database at {host!r} is stamped {found!r}. "
            f"Refusing to start. Point DATABASE_URL at the {expected!r} database, "
            f"or set APP_ENV={found!r} if you really mean to touch it."
        )
