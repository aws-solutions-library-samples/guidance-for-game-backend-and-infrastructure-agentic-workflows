"""Deterministic runtime-service tests for the E5 bounded-autonomy layer (#439).

``AutonomyRuntimeService`` is a pure, deterministic assembler. Given only
server-owned trusted inputs — a resolved autonomy policy, one canonical E1
observation, the exact six ADR 0001 authority inputs, the trusted automation
principal, and the strict durable window state — it produces the hash-bound v2
autonomous *decision* and the hash-bound v2 prepared *operation* defined by the
issue #438 contract, and nothing else. It performs no AWS, runtime, or
infrastructure work, reads no environment, holds no credential, and never
consults model or request-body input for identity, policy, limits,
authorization, playbook, executor, or credentials.

These tests assert:

* the produced documents pass the frozen #438 contract + binding validators;
* every hash is the canonical #438 digest (never recomputed here differently);
* the authorization is truthful — an authorized decision carries only
  ``APPROVED_AUTONOMOUS`` and never any human-approval field;
* determinism — identical inputs yield byte-identical documents and ids;
* fail-closed rejection of model/request identity, policy, limits, and
  executor-credential inputs.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_playbook_definition import autonomy_playbook_hash
from operations.autonomy_runtime import (
    AutonomyRuntimeError,
    AutonomyRuntimeInputs,
    AutonomyRuntimeService,
    PreparedAutonomousOperation,
)
from operations.contracts import load_json
from operations.contracts.autonomy import (
    AutonomyContractError,
    autonomous_decision_hash,
    autonomous_prepared_hash,
    validate_autonomous_operation_binding,
    validate_autonomy_contract,
)

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

# The evaluated instant matches the canonical decision fixture's evaluated_at.
_EVALUATED_AT = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


def _observation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-autonomy-observation.valid.json")


def _window_state() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")


def _decision_fixture() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomous-decision.valid.json")


def _operation_fixture() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomous-operation.valid.json")


def _inputs(**overrides: Any) -> AutonomyRuntimeInputs:
    kwargs: dict[str, Any] = {
        "policy": _policy(),
        "observation": _observation(),
        "authority_inputs": dict(_AUTHORITY),
        "automation_principal": dict(_PRINCIPAL),
        "window_state": _window_state(),
        "requested": {"desired": 1, "minimum": 0, "maximum": 1},
        "correlation": dict(_CORRELATION),
        "evaluated_at": _EVALUATED_AT,
    }
    kwargs.update(overrides)
    return AutonomyRuntimeInputs(**kwargs)


def _service() -> AutonomyRuntimeService:
    return AutonomyRuntimeService()


def _prepare(**overrides: Any) -> PreparedAutonomousOperation:
    return _service().prepare(_inputs(**overrides))


# -- Happy path: authorized decision + operation ----------------------------


@pytest.mark.unit
def test_authorized_decision_and_operation_pass_the_contract() -> None:
    result = _prepare()
    validate_autonomy_contract("gamelift-capacity-autonomous-decision", result.decision)
    validate_autonomy_contract("gamelift-capacity-autonomous-operation", result.operation)


@pytest.mark.unit
def test_produced_documents_pass_the_full_binding() -> None:
    result = _prepare()
    # Must bind against the exact trusted inputs with no drift anywhere.
    validate_autonomous_operation_binding(
        result.operation,
        result.decision,
        _policy(),
        _observation(),
        _window_state(),
    )


@pytest.mark.unit
def test_authorized_decision_is_truthful_and_has_no_human_approval() -> None:
    result = _prepare()
    assert result.decision["decision"] == "authorized"
    assert result.decision["reason_codes"] == ["APPROVED_AUTONOMOUS"]
    assert result.operation["authority"]["decision"] == "authorized"
    # There is no human-approval field anywhere in the autonomy layer.
    assert "approval" not in result.operation
    assert "approval" not in result.decision
    assert "approved_by" not in result.decision
    assert "granted_by" not in result.operation
    assert result.decision["required_execution_authority"] == "operate"
    assert result.operation["required_execution_authority"] == "operate"


@pytest.mark.unit
def test_hashes_are_the_canonical_438_digests() -> None:
    result = _prepare()
    assert result.decision_hash == autonomous_decision_hash(result.decision)
    assert result.prepared_hash == autonomous_prepared_hash(result.operation)
    assert result.operation["prepared_hash"] == result.prepared_hash
    assert result.operation["decision"]["decision_hash"] == result.decision_hash
    assert result.operation["playbook"]["playbook_hash"] == autonomy_playbook_hash()


@pytest.mark.unit
def test_decision_expiry_is_min_of_observation_expiry_and_policy_ttl() -> None:
    # evaluated_at 19:12:52 + ttl 120s = 19:14:52 but observation expires 19:13:52.
    result = _prepare()
    assert result.decision["decision_expires_at"] == "2026-09-21T19:13:52Z"
    assert result.operation["decision_expires_at"] == "2026-09-21T19:13:52Z"


@pytest.mark.unit
def test_decision_id_is_derived_from_operation_id() -> None:
    result = _prepare()
    assert result.decision["decision_id"] == f"autz.{result.operation['operation_id']}"
    assert result.operation["decision"]["decision_id"] == result.decision["decision_id"]


@pytest.mark.unit
def test_operation_principal_carries_policy_scope() -> None:
    result = _prepare()
    assert result.operation["automation_principal"] == {
        **_PRINCIPAL,
        "tenant_id": "tenant.default",
        "workspace_id": "workspace.default",
    }
    # The decision carries the bare three-field principal.
    assert result.decision["automation_principal"] == _PRINCIPAL


# -- Determinism -------------------------------------------------------------


@pytest.mark.unit
def test_identical_inputs_yield_byte_identical_documents() -> None:
    first = _prepare()
    second = _prepare()
    assert first.decision == second.decision
    assert first.operation == second.operation
    assert first.operation["operation_id"] == second.operation["operation_id"]
    assert first.operation["idempotency_token"] == second.operation["idempotency_token"]


# -- Denied path -------------------------------------------------------------


@pytest.mark.unit
def test_disabled_deployment_denies_without_authorization() -> None:
    authority = dict(_AUTHORITY)
    authority["deployment_mode"] = "disabled"
    result = _prepare(authority_inputs=authority)
    assert result.decision["decision"] == "denied"
    assert "DEPLOYMENT_DISABLED" in result.decision["reason_codes"]
    assert "APPROVED_AUTONOMOUS" not in result.decision["reason_codes"]
    assert result.operation["authority"]["decision"] == "denied"
    validate_autonomous_operation_binding(result.operation, result.decision, _policy(), _observation(), _window_state())


@pytest.mark.unit
def test_insufficient_authority_denies() -> None:
    authority = dict(_AUTHORITY)
    authority["principal_authority"] = "advise"
    result = _prepare(authority_inputs=authority)
    assert result.decision["decision"] == "denied"
    assert "INSUFFICIENT_AUTHORITY" in result.decision["reason_codes"]


@pytest.mark.unit
def test_budget_exhaustion_denies_closed() -> None:
    window = _window_state()
    window["window_micro_usd"] = 10_000_000_000
    # Recompute the state hash so the strict window state stays contract-valid.
    # Local modules
    from operations.contracts.autonomy import autonomy_window_state_hash

    window["state_hash"] = autonomy_window_state_hash(window)
    result = _service().prepare(_inputs(window_state=window))
    assert result.decision["decision"] == "denied"
    assert "BUDGET_EXCEEDED" in result.decision["reason_codes"]


# -- Fail-closed on forbidden inputs ----------------------------------------


@pytest.mark.unit
def test_model_or_request_identity_in_principal_is_rejected() -> None:
    principal = dict(_PRINCIPAL)
    principal["source_type"] = "user"  # not automation
    with pytest.raises((AutonomyRuntimeError, AutonomyContractError)):
        _prepare(automation_principal=principal)


@pytest.mark.unit
def test_extra_credential_field_on_inputs_is_rejected() -> None:
    with pytest.raises(TypeError):
        AutonomyRuntimeInputs(  # type: ignore[call-arg]
            policy=_policy(),
            observation=_observation(),
            authority_inputs=dict(_AUTHORITY),
            automation_principal=dict(_PRINCIPAL),
            window_state=_window_state(),
            requested={"desired": 1, "minimum": 0, "maximum": 1},
            correlation=dict(_CORRELATION),
            evaluated_at=_EVALUATED_AT,
            executor_credential="AKIA-not-allowed",
        )


@pytest.mark.unit
def test_naive_evaluated_at_is_rejected() -> None:
    with pytest.raises((AutonomyRuntimeError, ValueError)):
        _prepare(evaluated_at=datetime(2026, 9, 21, 19, 12, 52))


@pytest.mark.unit
def test_tampered_policy_hash_fails_closed() -> None:
    policy = _policy()
    policy["policy_hash"] = "sha256:" + "0" * 64
    with pytest.raises(AutonomyContractError):
        _prepare(policy=policy)


@pytest.mark.unit
def test_service_holds_no_provider_write_or_executor_credential() -> None:
    service = _service()
    # The service is a pure assembler; it exposes no dispatch/execute/write path.
    for forbidden in ("execute", "dispatch", "invoke", "update_fleet_capacity", "client"):
        assert not hasattr(service, forbidden)
