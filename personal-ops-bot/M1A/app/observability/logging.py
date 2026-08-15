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

2. **Redaction.** A processor drops known-sensitive keys before rendering,
   recursively through nested mappings and sequences up to `MAX_REDACT_DEPTH`.
   Defence in depth: `SecretStr` already protects configured secrets, but this
   catches the case where someone logs a dict that happens to contain one.
   It is *not* the primary protection and must not be treated as one -- it
   knows a fixed list of key names, so a secret under an unlisted key, or one
   interpolated into a message string, still reaches the log.

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
from collections.abc import Mapping, MutableMapping
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

# How far into a nested structure we look for sensitive keys.
#
# A cap rather than unbounded recursion because a logging processor has one
# hard requirement: it must never be the thing that crashes the process.
# Unbounded recursion on a deeply nested payload raises RecursionError *inside
# the log call*, and a self-referential dict (`d["self"] = d`) never terminates
# at all. The cap handles both with one mechanism, so there is no separate
# cycle detector to get wrong.
#
# At the cap we substitute a marker rather than returning the value unscanned.
# That is the difference between a cap that is safe and one that merely moves
# the bug: returning the subtree would (a) put data we never examined into the
# log, which is exactly the guarantee this module is supposed to make, and
# (b) hand a cycle to the JSON renderer, which raises on circular references.
#
# The tradeoff, stated plainly: data nested deeper than this is not rendered.
# It is replaced by a visible marker, so the loss is obvious in the output
# rather than silent. Real structured-log payloads are far shallower, and a
# value buried nine levels down means the call site is logging an object graph,
# which is its own bug.
MAX_REDACT_DEPTH = 8
TRUNCATED = "<truncated>"


def _is_sensitive(key: Any) -> bool:
    """Keys can be non-strings (ints, tuples); only strings are ever sensitive."""
    return isinstance(key, str) and key.lower() in REDACTED_KEYS


def _redact_value(value: Any, depth: int) -> Any:
    """Return `value` with sensitive entries replaced, recursing into containers.

    Containers are rebuilt rather than mutated. That matters: the caller owns
    the object it passed to `log.info(...)`, and a logging call that quietly
    rewrote the application's own dict would be a genuinely nasty bug to chase.
    """
    if depth >= MAX_REDACT_DEPTH:
        return TRUNCATED
    if isinstance(value, Mapping):
        return {
            key: (REDACTED if _is_sensitive(key) else _redact_value(item, depth + 1))
            for key, item in value.items()
        }
    # str/bytes are Sequences too; recursing into them would walk characters.
    if isinstance(value, list | tuple):
        items = [_redact_value(item, depth + 1) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    return value


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace values of sensitive keys before rendering, at any depth up to
    `MAX_REDACT_DEPTH`, through nested mappings and lists alike.

    The event dict itself is structlog's, so mutating it in place is correct;
    everything below it is rebuilt (see `_redact_value`).
    """
    for key, value in list(event_dict.items()):
        event_dict[key] = REDACTED if _is_sensitive(key) else _redact_value(value, depth=1)
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
