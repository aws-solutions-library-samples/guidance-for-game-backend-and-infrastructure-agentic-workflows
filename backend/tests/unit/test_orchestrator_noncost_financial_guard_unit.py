"""The orchestrator withholds volunteered financial content on a non-cost query (#464).

The orchestrator's last line of defense for a non-cost request is a guard over
the model's own prose: if the routing model volunteers an unvalidated monetary
figure for a query the classifier did not treat as a cost topic, that figure
must not escape. The call site is the ``elif
contains_unvalidated_financial_content(str(response))`` branch in
``run_orchestrator``; deleting it leaves the whole unit suite otherwise green,
so this test pins that branch directly.

The project autouse fixture replaces the ``agents.orchestrator.run_orchestrator``
module attribute with a mock, so a test that calls it through the module reaches
the mock, not the branch. This module binds the real callable at import time —
before any per-test fixture runs — and calls that reference, which the per-test
patch of the module attribute does not rebind.
"""

# Standard library
from typing import Any
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
# Bind the real callable before the autouse fixture patches the module attribute
# for each test.
from agents.orchestrator import run_orchestrator as _real_run_orchestrator

pytestmark = pytest.mark.unit


class _FakeAgent:
    """Stands in for the Strands agent; returns a fixed response for any query."""

    def __init__(self, response: str) -> None:
        self._response = response

    def __call__(self, query: Any) -> str:
        return self._response


def _run_with_model_response(query: str, response: str) -> str:
    agent = _FakeAgent(response)
    with (
        patch("agents.orchestrator.Agent", lambda *a, **k: agent),
        patch("agents.orchestrator.USE_BEDROCK_SESSIONS", False),
    ):
        return str(_real_run_orchestrator(query, context=None))


class TestOrchestratorNonCostFinancialGuard:
    def test_unvalidated_amount_on_noncost_query_is_withheld(self) -> None:
        # A non-cost operational query (fleet discovery) whose model answer
        # volunteers a dollar amount. The guard must replace it.
        model_response = "## GameLift Fleets\n\nYour production fleet costs $4,200.00 per month to run."
        result = _run_with_model_response("show me my gamelift fleets", model_response)

        assert "$4,200.00" not in result
        # The deterministic withholding/advisory replacement is returned instead.
        assert "withheld" in result.lower() or "cost report" in result.lower()

    def test_operational_answer_without_money_is_preserved(self) -> None:
        # The guard must not fire on an ordinary operational answer with no
        # financial value, so the branch is not vacuously rewriting everything.
        model_response = "## GameLift Fleets\n\nYou have 2 fleets: 1 classic (ACTIVE) and 1 container (ACTIVE)."
        result = _run_with_model_response("show me my gamelift fleets", model_response)

        assert result == model_response
