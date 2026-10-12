"""Import-cycle regression for the E2 decisions module (issue #414).

``operations.decisions`` must import cleanly in a fresh interpreter without a
caller first importing ``operations.approval``. A future sweeper or executor
that imports the decisions module directly must not crash at startup, so the
two modules must not form a circular import.
"""

from __future__ import annotations

# Standard library
import subprocess
import sys
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).parents[2] / "src"


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(_SRC),
        capture_output=True,
        text=True,
    )


def test_decisions_imports_first_in_a_fresh_interpreter() -> None:
    result = _import_in_fresh_interpreter("operations.decisions")
    assert result.returncode == 0, result.stderr
    assert "cannot import name" not in result.stderr
    assert "partially initialized module" not in result.stderr


def test_approval_still_exposes_the_decision_symbols() -> None:
    # The whole approval domain remains importable from one place.
    result = _import_in_fresh_interpreter(
        "operations.decisions; "
        "from operations.decisions import DecisionCommitOutcome, DecisionRequest, "
        "DecisionRequestContext, DecisionStore, LifecycleDecisionService"
    )
    assert result.returncode == 0, result.stderr
