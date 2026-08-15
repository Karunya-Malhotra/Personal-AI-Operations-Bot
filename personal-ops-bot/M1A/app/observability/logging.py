"""Structured logging.

Why structured logs from the very first milestone, before there is anything
interesting to log:

The architecture promises we can answer "why did the bot say that?" by joining
`agent_runs` -> `llm_calls` -> `tool_calls`. That is the *durable* trace. Logs
are the *live* view of the same thing, and they are only useful for it if every
line carries the correlation ids. Retrofitting `run_id` into a codebase that
logs f-strings means touching every call site. Adding it now costs one file.

Three mechanisms, in order of importance:

1. **contextvars.** `bind_contextvars(run_id=...)` attaches a value to the
   current async task, and every subsequent log line in that task -- including
   ones emitted five layers down in a repository -- carries it automatically.
   This is what makes correlation free at the call site.

2. **Redaction.** A processor drops known-sensitive keys before rendering.
   Defence in depth: `SecretStr` already protects configured secrets, but this
   catches the case where someone logs a dict that happens to contain one.

3. **Renderer by environment.** Console-with-colour in dev because you read it
   with your eyes; JSON in prod because something else reads it. Same events,
   same keys, different rendering -- so a bug you debug locally produces the
   same fields in production.

Note this module takes primitives, not `Settings`. That keeps `observability`
below `config` in the layer order, so a future worker or script can configure
logging without constructing the whole settings object.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars

__all__ = [
    "bind_contextvars",
    "clear_contextvars",
    "configure_logging",
    "get_logger",
    "unbind_contextvars",
]

# Keys whose values are never rendered, wherever they appear in an event dict.
REDACTED_KEYS = frozenset(
    {
        "password",
        "token",
        "api_key",
        "apikey",
        "secret",
        "authorization",
        "cookie",
        "database_url",
        "dsn",
        "access_token",
        "refresh_token",
        "bot_token",
        "app_secret",
        "credential",
    }
)

REDACTED = "<redacted>"


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace values of sensitive keys, at any depth, before rendering."""
    for key, value in list(event_dict.items()):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = REDACTED
        elif isinstance(value, dict):
            event_dict[key] = {
                k: (REDACTED if k.lower() in REDACTED_KEYS else v) for k, v in value.items()
            }
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    """Configure structlog and the stdlib root logger as a single pipeline.

    The stdlib half is not optional. uvicorn, SQLAlchemy and asyncpg all log
    through `logging`; without routing them through the same renderer, half the
    output is JSON and half is not, which defeats the purpose of structured logs
    the first time you grep production.

    The mechanism is structlog's `ProcessorFormatter`: our loggers wrap their
    event dict and hand it to stdlib, foreign stdlib records get the same
    `foreign_pre_chain` applied, and both are rendered by the same renderer at
    the end. One pipeline, two entrances.
    """
    numeric_level = logging.getLevelNamesMapping()[level.upper()]

    # Applied to events from BOTH structlog and plain stdlib loggers.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,  # correlation ids, injected here
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # uvicorn installs its own handlers; make them defer to ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
