"""Logging emits machine-readable events and never renders secrets."""

from __future__ import annotations

import json

from app.observability.logging import (
    bind_contextvars,
    clear_contextvars,
    configure_logging,
    get_logger,
)


def test_json_output_has_standard_keys(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging(level="INFO", json_output=True)
    get_logger("test").info("thing.happened", count=3)
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["event"] == "thing.happened"
    assert payload["level"] == "info"
    assert payload["count"] == 3
    assert "timestamp" in payload


def test_contextvars_propagate_without_being_passed(capsys) -> None:  # type: ignore[no-untyped-def]
    """This is what makes correlation free at the call site: a repository five
    layers down logs `run_id` without knowing it exists."""
    configure_logging(level="INFO", json_output=True)
    clear_contextvars()
    bind_contextvars(run_id="run-123", conversation_id="conv-9")
    try:
        get_logger("deep.module").info("tool.executed")
        payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert payload["run_id"] == "run-123"
        assert payload["conversation_id"] == "conv-9"
    finally:
        clear_contextvars()


def test_sensitive_keys_are_redacted(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging(level="INFO", json_output=True)
    get_logger("test").info(
        "config.loaded",
        database_url="postgresql://user:hunter2@host/db",
        nested={"api_key": "sk-secret", "safe": "visible"},
    )
    line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "hunter2" not in line
    assert "sk-secret" not in line
    payload = json.loads(line)
    assert payload["database_url"] == "<redacted>"
    assert payload["nested"]["api_key"] == "<redacted>"
    assert payload["nested"]["safe"] == "visible"
