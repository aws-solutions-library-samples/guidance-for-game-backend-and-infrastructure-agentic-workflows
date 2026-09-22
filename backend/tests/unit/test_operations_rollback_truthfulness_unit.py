"""Rollback visibility is truthful, never inferred (issue #416, E4).

The detail projection must not INFER a rollback from a failed state. If no
explicit rollback event is recorded in the operation's evidence ledger, the
projection exposes ``rollback: {applicable: false, outcome: not_recorded}`` — an
honest "we did not record a rollback" — rather than fabricating an
applicable-and-pending rollback. When a rollback event IS recorded, the
projection reports it truthfully (succeeded / failed / pending).
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.control.projections import _rollback_visibility

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def test_failed_without_rollback_event_is_not_recorded() -> None:
    # A failed execution with no recorded rollback must NOT infer a pending
    # rollback; it reports not_recorded honestly.
    visibility = _rollback_visibility("failed", ledger=[])
    assert visibility == {"applicable": False, "outcome": "not_recorded"}


def test_recorded_rollback_success_is_reported() -> None:
    ledger = [{"event_type": "rollback_succeeded", "sequence": 5}]
    visibility = _rollback_visibility("failed", ledger=ledger)
    assert visibility == {"applicable": True, "outcome": "succeeded"}


def test_recorded_rollback_failure_is_reported() -> None:
    ledger = [{"event_type": "rollback_failed", "sequence": 5}]
    visibility = _rollback_visibility("failed", ledger=ledger)
    assert visibility == {"applicable": True, "outcome": "failed"}


def test_recorded_rollback_pending_is_reported() -> None:
    ledger = [{"event_type": "rollback_started", "sequence": 5}]
    visibility = _rollback_visibility("executing", ledger=ledger)
    assert visibility == {"applicable": True, "outcome": "pending"}


def test_healthy_state_without_rollback_is_not_applicable() -> None:
    # A non-failed, non-rollback operation legitimately has no rollback facet.
    visibility = _rollback_visibility("succeeded", ledger=[])
    assert visibility == {"applicable": False, "outcome": "not_applicable"}
