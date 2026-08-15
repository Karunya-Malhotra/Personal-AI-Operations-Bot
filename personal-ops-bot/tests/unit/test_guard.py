"""The dev/prod guard's decision logic.

Every case here runs without a database, because `evaluate_stamp` is pure. That
separation is the reason this file is fast and exhaustive rather than slow and
representative.
"""

from __future__ import annotations

import pytest

from app.core.environment import Environment
from app.core.errors import ConfigurationError, EnvironmentMismatchError
from app.db.guard import evaluate_stamp


def test_matching_stamp_passes() -> None:
    evaluate_stamp(expected=Environment.DEV, found="dev", host="localhost")
    evaluate_stamp(expected=Environment.PROD, found="prod", host="db.internal")


def test_dev_process_against_prod_database_is_refused() -> None:
    """The accident this whole mechanism exists for."""
    with pytest.raises(EnvironmentMismatchError) as exc:
        evaluate_stamp(expected=Environment.DEV, found="prod", host="db.internal")
    assert exc.value.expected == "dev"
    assert exc.value.found == "prod"
    assert "db.internal" in str(exc.value)


def test_prod_process_against_dev_database_is_also_refused() -> None:
    """Both directions. This one means a deploy is misconfigured and would
    otherwise quietly serve an empty database."""
    with pytest.raises(EnvironmentMismatchError):
        evaluate_stamp(expected=Environment.PROD, found="dev", host="localhost")


def test_unstamped_database_is_refused() -> None:
    """An unstamped database is what a freshly restored production dump looks
    like. 'Probably fine' is not an acceptable reading."""
    with pytest.raises(ConfigurationError, match="no 'environment' stamp"):
        evaluate_stamp(expected=Environment.DEV, found=None, host="localhost")


def test_unknown_stamp_value_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="not a known environment"):
        evaluate_stamp(expected=Environment.DEV, found="staging", host="localhost")


def test_error_message_never_contains_credentials() -> None:
    """The guard reports a host, never a DSN. Checked so a future 'helpful'
    edit that includes the connection string fails here."""
    with pytest.raises(EnvironmentMismatchError) as exc:
        evaluate_stamp(expected=Environment.DEV, found="prod", host="db.internal")
    assert "://" not in str(exc.value)
