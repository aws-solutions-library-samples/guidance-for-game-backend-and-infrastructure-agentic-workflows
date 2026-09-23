"""Pinned-vector faithfulness test for the E5 runtime assembler (#439).

The runtime derives its own deterministic ``operation_id`` from the trusted
idempotent intent, so its documents never carry the canonical fixtures'
*placeholder* id (``op_bbb...``) and therefore never reproduce the pinned
``decision_hash`` / ``prepared_hash`` verbatim — those pinned hashes bind the
placeholder id.

What this test pins instead is faithfulness: every field the runtime assembles
is byte-identical to the canonical fixture **except** the derived id and the
hashes that bind it. Concretely, re-stamping the produced documents with the
fixture's operation/decision id reproduces the pinned #438 hashes exactly, which
proves the assembler introduces no drift in any semantic field.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime import AutonomyRuntimeInputs, AutonomyRuntimeService
from operations.contracts import load_json
from operations.contracts.autonomy import autonomous_decision_hash, autonomous_prepared_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"

_AUTHORITY = {
    "deployment_mode": "operate",
    "tenant_policy": "operate",
    "workspace_policy": "operate",
    "principal_authority": "operate",
    "capability_maximum": "operate",
    "operation_risk_policy": "operate",
}
_PRINCIPAL = {
    "source_type": "automation",
    "subject_id": "subject.autonomy-agent",
    "client_id": "client.autonomy-runtime",
}
_CORRELATION = {"correlation_id": "corr.autonomy-1", "request_id": "request.autonomy-1"}
_EVALUATED_AT = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)

_FIXTURE_OPERATION_ID = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_FIXTURE_DECISION_ID = "autz.op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_FIXTURE_IDEMPOTENCY = "idem_bbbbbbbbbbbbbbbbbbbbbbbb"


def _vectors() -> dict[str, Any]:
    return load_json(FIXTURES / "autonomy-contract-vectors.json")["expected"]


def _prepared() -> Any:
    return AutonomyRuntimeService().prepare(
        AutonomyRuntimeInputs(
            policy=load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json"),
            observation=load_json(FIXTURES / "gamelift-autonomy-observation.valid.json"),
            authority_inputs=dict(_AUTHORITY),
            automation_principal=dict(_PRINCIPAL),
            window_state=load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json"),
            requested={"desired": 1, "minimum": 0, "maximum": 1},
            correlation=dict(_CORRELATION),
            evaluated_at=_EVALUATED_AT,
        )
    )


@pytest.mark.unit
def test_only_the_derived_id_and_its_bound_hashes_differ_from_the_fixture() -> None:
    result = _prepared()
    decision_fixture = load_json(FIXTURES / "gamelift-capacity-autonomous-decision.valid.json")
    operation_fixture = load_json(FIXTURES / "gamelift-capacity-autonomous-operation.valid.json")

    # Re-stamp the produced documents with the fixture placeholder identifiers.
    decision = dict(result.decision)
    decision["decision_id"] = _FIXTURE_DECISION_ID
    operation = dict(result.operation)
    operation["operation_id"] = _FIXTURE_OPERATION_ID
    operation["idempotency_token"] = _FIXTURE_IDEMPOTENCY
    operation["decision"] = {
        "decision_id": _FIXTURE_DECISION_ID,
        "decision_hash": autonomous_decision_hash(decision),
    }
    operation["prepared_hash"] = autonomous_prepared_hash(operation)

    # With the fixture identifiers, every other assembled field is byte-identical.
    assert decision == decision_fixture
    assert operation == operation_fixture


@pytest.mark.unit
def test_restamped_documents_reproduce_the_pinned_438_hashes() -> None:
    result = _prepared()
    expected = _vectors()

    decision = dict(result.decision)
    decision["decision_id"] = _FIXTURE_DECISION_ID
    assert autonomous_decision_hash(decision) == expected["decision_hash"]

    operation = dict(result.operation)
    operation["operation_id"] = _FIXTURE_OPERATION_ID
    operation["idempotency_token"] = _FIXTURE_IDEMPOTENCY
    operation["decision"] = {
        "decision_id": _FIXTURE_DECISION_ID,
        "decision_hash": expected["decision_hash"],
    }
    assert autonomous_prepared_hash(operation) == expected["prepared_hash"]


@pytest.mark.unit
def test_semantic_fields_match_the_frozen_decision_fixture() -> None:
    result = _prepared()
    decision_fixture = load_json(FIXTURES / "gamelift-capacity-autonomous-decision.valid.json")
    assert result.decision["current_state"] == decision_fixture["current_state"]
    assert result.decision["calculated_risk"] == decision_fixture["calculated_risk"]
    assert result.decision["decision_expires_at"] == decision_fixture["decision_expires_at"]
    assert result.decision["reason_codes"] == decision_fixture["reason_codes"]
