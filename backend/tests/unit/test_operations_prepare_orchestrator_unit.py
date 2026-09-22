"""Unit tests for the E2 prepare orchestrator (issue #414).

The orchestrator ties the real AdviceService and PrepareService together and
persists the resulting immutable prepared operation through the approval store's
atomic ``persist_prepared_operation`` transaction. These tests drive the real
advice/prepare services against fake ports plus a fake persist store and assert:

* a healthy in-bounds proposal is prepared, requires approval, and is persisted
  once, returning the stored prepared operation and its bound hash;
* a byte-identical retry of the same intent replays the stored operation (no
  second persist writes new content) and returns the same hash;
* a changed intent under the same idempotency token conflicts;
* a deterministically denied proposal is NOT persisted;
* the persisted state-change / ledger records are server-owned and valid.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.advice import (
    AdviceService,
    CapacityBounds,
    CapacityValues,
    CurrentCapacity,
)
from operations.approval import StoredPreparedOperation
from operations.approval_store import PersistOutcome, PersistResult
from operations.contracts import validate_contract
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal
from operations.prepare import CapacityPlaybook, PreparedDecision, PrepareService
from operations.prepare_orchestrator import (
    PrepareOrchestrator,
    PrepareOrchestratorError,
)
from operations.settings import resolve_operations_settings

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
OBS_HASH = "sha256:" + "b" * 64


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject_id="user.requester",
        client_id="client.requester",
        audience="operations-api",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=NOW + timedelta(hours=1),
    )


def _boundary() -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"client.requester"}),
        approver_client_ids=frozenset({"client.approver"}),
        trusted_audiences=frozenset({"operations-api"}),
    )


def _current(desired: int = 10) -> CurrentCapacity:
    return CurrentCapacity(
        observation_id=OBS_ID,
        observation_hash=OBS_HASH,
        capacity=CapacityValues(desired=desired, minimum=2, maximum=20),
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def _bounds(enrolled: bool = True) -> CapacityBounds:
    return CapacityBounds(
        floor=1,
        ceiling=30,
        max_step=10,
        enrollment_id="enroll.fleet-default",
        enrollment_version="2026-09-01",
        policy_id="policy.capacity-default",
        policy_version="2026-09-01",
        target_enrolled=enrolled,
    )


class FakeStatePort:
    def __init__(self, current: CurrentCapacity | None) -> None:
        self._current = current

    def load_current_capacity(self, *, requester, fleet_id, location):
        return self._current


class FakeBoundsPort:
    def __init__(self, bounds: CapacityBounds) -> None:
        self._bounds = bounds

    def resolve_bounds(self, *, requester, fleet_id, location):
        return self._bounds


class FakePersistStore:
    """A minimal fake of the approval store's persist transaction."""

    def __init__(self) -> None:
        self.by_token: dict[str, dict[str, Any]] = {}
        self.by_operation: dict[str, dict[str, Any]] = {}
        self.persist_calls = 0

    def persist_prepared_operation(
        self,
        *,
        prepared_operation,
        prepared_hash,
        workspace_id,
        idempotency_token,
        idempotency_fingerprint,
        state_change,
        ledger_event,
        commit_not_after,
    ) -> PersistResult:
        self.persist_calls += 1
        key = f"{workspace_id}#{idempotency_token}"
        existing = self.by_token.get(key)
        if existing is None:
            self.by_token[key] = {
                "operation_id": prepared_operation["operation_id"],
                "fingerprint": idempotency_fingerprint,
                "state_change": state_change,
                "ledger_event": ledger_event,
            }
            self.by_operation[prepared_operation["operation_id"]] = {
                "operation": dict(prepared_operation),
                "prepared_hash": prepared_hash,
                "state": "pending_approval",
            }
            return PersistResult(PersistOutcome.PERSISTED, operation_id=prepared_operation["operation_id"])
        if existing["fingerprint"] != idempotency_fingerprint:
            return PersistResult(PersistOutcome.INTENT_CONFLICT)
        return PersistResult(PersistOutcome.REPLAYED, operation_id=existing["operation_id"])

    def load_for_approval(self, operation_id: str) -> StoredPreparedOperation | None:
        record = self.by_operation.get(operation_id)
        if record is None:
            return None
        return StoredPreparedOperation(record["operation"], record["prepared_hash"], record["state"])


def _advice_service(state: FakeStatePort, bounds: FakeBoundsPort, mode: str = "advise") -> AdviceService:
    return AdviceService(
        settings=resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": mode}),
        identity_boundary=_boundary(),
        state_port=state,
        bounds_port=bounds,
        clock=lambda: NOW,
    )


def _prepare_service() -> PrepareService:
    return PrepareService(
        settings=resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "advise"}),
        identity_boundary=_boundary(),
        playbook=CapacityPlaybook(
            playbook_id="playbook.gamelift-capacity",
            playbook_version="1.0.0",
            playbook_hash="sha256:" + "1" * 64,
            profile="gamelift.capacity-adjustment/1.0",
            retry_policy={
                "max_attempts": 3,
                "base_delay_seconds": 2,
                "max_delay_seconds": 60,
                "reconcile_before_retry": True,
            },
            future_executor_binding={
                "executor_id": "executor.gamelift-capacity",
                "executor_binding_version": "1.0",
            },
        ),
        clock=lambda: NOW,
    )


def _orchestrator(state: FakeStatePort, bounds: FakeBoundsPort, store: FakePersistStore) -> PrepareOrchestrator:
    return PrepareOrchestrator(
        advice_service=_advice_service(state, bounds, mode="operate"),
        prepare_service=_prepare_service(),
        store=store,
        clock=lambda: NOW,
        deployment_mode="operate",
    )


def _proposal_body(desired: int = 14) -> dict[str, Any]:
    return {
        "request_contract_version": "1.0",
        "capability_id": "gamelift.capacity-adjustment",
        "idempotency_token": TOKEN,
        "observation_id": OBS_ID,
        "proposal": {
            "fleet_id": FLEET,
            "location": LOCATION,
            "requested": {"desired": desired, "minimum": 2, "maximum": 20},
        },
    }


def test_healthy_proposal_is_prepared_and_persisted_once() -> None:
    store = FakePersistStore()
    orch = _orchestrator(FakeStatePort(_current()), FakeBoundsPort(_bounds()), store)
    result = orch.prepare(_proposal_body(), _principal(), request_id="request.p1", correlation_id="corr.p1")
    assert result.decision is PreparedDecision.APPROVAL_REQUIRED
    assert result.persisted is True
    assert result.operation["operation_id"].startswith("op_")
    assert result.prepared_hash == result.operation["prepared_hash"]
    assert store.persist_calls == 1
    # The persisted initial records are server-owned and schema-valid.
    stored = store.by_token[f"workspace.default#{TOKEN}"]
    validate_contract("operation-state-change", stored["state_change"])
    validate_contract("ledger-event", stored["ledger_event"])
    assert stored["state_change"]["new_state"] == "pending_approval"


def test_identical_retry_replays_same_operation() -> None:
    store = FakePersistStore()
    orch = _orchestrator(FakeStatePort(_current()), FakeBoundsPort(_bounds()), store)
    first = orch.prepare(_proposal_body(), _principal(), request_id="request.p1", correlation_id="corr.p1")
    second = orch.prepare(_proposal_body(), _principal(), request_id="request.p2", correlation_id="corr.p2")
    assert second.operation["operation_id"] == first.operation["operation_id"]
    assert second.prepared_hash == first.prepared_hash
    assert second.replayed is True


def test_changed_intent_same_token_conflicts() -> None:
    store = FakePersistStore()
    orch = _orchestrator(FakeStatePort(_current()), FakeBoundsPort(_bounds()), store)
    orch.prepare(_proposal_body(desired=14), _principal(), request_id="request.p1", correlation_id="corr.p1")
    with pytest.raises(PrepareOrchestratorError) as exc:
        orch.prepare(_proposal_body(desired=16), _principal(), request_id="request.p2", correlation_id="corr.p2")
    assert exc.value.error_code == "idempotency_conflict"


def test_denied_proposal_is_not_persisted() -> None:
    store = FakePersistStore()
    # Not enrolled => deterministic denial.
    orch = _orchestrator(FakeStatePort(_current()), FakeBoundsPort(_bounds(enrolled=False)), store)
    result = orch.prepare(_proposal_body(), _principal(), request_id="request.p1", correlation_id="corr.p1")
    assert result.decision is PreparedDecision.DENIED
    assert result.persisted is False
    assert store.persist_calls == 0


def test_injected_identity_field_in_body_is_rejected() -> None:
    store = FakePersistStore()
    orch = _orchestrator(FakeStatePort(_current()), FakeBoundsPort(_bounds()), store)
    body = _proposal_body()
    body["requester"] = {"subject_id": "attacker"}
    with pytest.raises(PrepareOrchestratorError) as exc:
        orch.prepare(body, _principal(), request_id="request.p1", correlation_id="corr.p1")
    assert exc.value.error_code == "contract_invalid"
