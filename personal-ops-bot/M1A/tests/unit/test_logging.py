"""Logging emits machine-readable events and never renders secrets."""

from __future__ import annotations

import json

from app.observability.logging import (
    MAX_REDACT_DEPTH,
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


# ---------------------------------------------------------------------------
# Redaction depth.
#
# These exist because the docstring used to promise redaction "at any depth"
# while the implementation looked exactly one level down and never entered a
# list. The promise was the part that was true in review and false in
# production, so each shape below is now pinned by a test that asserts on the
# *rendered line* -- what actually reaches the log -- rather than on the event
# dict, which is the thing the old code got right.
# ---------------------------------------------------------------------------


def _log_and_read(capsys, **kwargs) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
    """Emit one event and return (raw rendered line, parsed payload)."""
    configure_logging(level="INFO", json_output=True)
    get_logger("test").info("event", **kwargs)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    return line, json.loads(line)


def test_redacts_flat_sensitive_key(capsys) -> None:  # type: ignore[no-untyped-def]
    line, payload = _log_and_read(capsys, password="hunter2")
    assert "hunter2" not in line
    assert payload["password"] == "<redacted>"


def test_redacts_one_level_nested_dict(capsys) -> None:  # type: ignore[no-untyped-def]
    line, payload = _log_and_read(capsys, request={"token": "sk-one-level"})
    assert "sk-one-level" not in line
    assert payload["request"]["token"] == "<redacted>"


def test_redacts_deeply_nested_dict(capsys) -> None:  # type: ignore[no-untyped-def]
    """The case the old implementation leaked: sensitive key below depth 1."""
    line, payload = _log_and_read(
        capsys,
        request={"metadata": {"credentials": {"api_key": "sk-deep-secret"}}},
    )
    assert "sk-deep-secret" not in line
    assert payload["request"]["metadata"]["credentials"]["api_key"] == "<redacted>"


def test_redacts_inside_a_list(capsys) -> None:  # type: ignore[no-untyped-def]
    """The other case it leaked: the old code never entered a sequence."""
    line, payload = _log_and_read(capsys, items=[{"api_key": "sk-in-list"}])
    assert "sk-in-list" not in line
    assert payload["items"][0]["api_key"] == "<redacted>"


def test_redacts_through_dict_list_dict_nesting(capsys) -> None:  # type: ignore[no-untyped-def]
    line, payload = _log_and_read(
        capsys,
        response={"results": [{"user": {"secret": "sk-mixed"}}]},
    )
    assert "sk-mixed" not in line
    assert payload["response"]["results"][0]["user"]["secret"] == "<redacted>"


def test_non_sensitive_nested_data_is_preserved(capsys) -> None:  # type: ignore[no-untyped-def]
    """Redaction must not become "mangle the payload"; everything else survives."""
    _, payload = _log_and_read(
        capsys,
        request={
            "path": "/health",
            "attempts": 3,
            "tags": ["a", "b"],
            "nested": {"ok": True, "ratio": 1.5, "missing": None},
        },
    )
    assert payload["request"] == {
        "path": "/health",
        "attempts": 3,
        "tags": ["a", "b"],
        "nested": {"ok": True, "ratio": 1.5, "missing": None},
    }


def test_sensitive_key_matching_is_case_insensitive_at_depth(capsys) -> None:  # type: ignore[no-untyped-def]
    line, payload = _log_and_read(
        capsys,
        outer={"Authorization": "Bearer sk-cased", "API_Key": "sk-upper"},
    )
    assert "sk-cased" not in line
    assert "sk-upper" not in line
    assert payload["outer"]["Authorization"] == "<redacted>"
    assert payload["outer"]["API_Key"] == "<redacted>"


def test_non_string_keys_do_not_crash_the_log_call(capsys) -> None:  # type: ignore[no-untyped-def]
    """Dict keys need not be strings. The old code called `.lower()` on them
    unconditionally, so logging `{"counts": {1: "x"}}` raised AttributeError
    from inside the logging pipeline."""
    _, payload = _log_and_read(capsys, counts={1: "one", "token": "sk-mixed-keys"})
    assert payload["counts"]["token"] == "<redacted>"
    assert payload["counts"]["1"] == "one"  # json turns the int key into a string


def test_redaction_does_not_mutate_the_callers_object(capsys) -> None:  # type: ignore[no-untyped-def]
    """A log call must not rewrite application state it was merely shown."""
    payload_arg = {"api_key": "sk-owned-by-caller"}
    _log_and_read(capsys, data=payload_arg)
    assert payload_arg == {"api_key": "sk-owned-by-caller"}


def test_self_referential_structure_terminates(capsys) -> None:  # type: ignore[no-untyped-def]
    """A cycle must hit the depth cap, not hang, recurse away, or raise.

    Without the marker substitution at the cap this also breaks the *renderer*:
    `json.dumps` raises ValueError on a circular reference, which would turn a
    log call into an exception.
    """
    cyclic: dict = {"password": "hunter2"}
    cyclic["self"] = cyclic
    line, _ = _log_and_read(capsys, data=cyclic)
    assert "hunter2" not in line


def test_below_the_depth_cap_nothing_is_emitted_unscanned(capsys) -> None:  # type: ignore[no-untyped-def]
    """Past MAX_REDACT_DEPTH we substitute a marker rather than the subtree.

    Returning the subtree unscanned would be the same class of bug this whole
    change fixes: a value reaching the log that redaction never looked at.
    """
    deep: dict = {"secret": "sk-past-the-cap"}
    for _ in range(MAX_REDACT_DEPTH + 2):
        deep = {"wrap": deep}
    line, _ = _log_and_read(capsys, data=deep)
    assert "sk-past-the-cap" not in line
    assert "<truncated>" in line
