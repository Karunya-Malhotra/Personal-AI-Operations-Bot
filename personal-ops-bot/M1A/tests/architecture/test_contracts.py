"""The layering is checked by CI, and this makes it fail the test run too.

Running `lint-imports` as a test means a violation shows up in the same place
as every other failure, rather than only in a CI step someone might not read.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.architecture
def test_import_contracts_hold() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "lint-imports"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
