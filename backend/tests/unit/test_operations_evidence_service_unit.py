"""Unit tests for the bounded, workspace-scoped E2 evidence service (issue #414).

``E2EvidenceService`` returns a bounded evidence view of one prepared operation
for a ``GET`` request: a preview of the operation, its current state, the
approval record when present, a bounded ledger, and an identifier-only future
executor handoff. It enforces trusted workspace ownership and never emits a
credential, an executor secret, or raw provider payload.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import load_json
from operations.evidence import E2EvidenceService, OperationEvidence
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
NOW = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)


def _operation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")


def _principal(workspace_id: str = None) -> VerifiedPrincipal:
    operation = _operation()
    return VerifiedPrincipal(
        subject_id="user.viewer",
        client_id=operation["requester"]["client_id"],
        audience="operations-api",
        tenant_id=operation["requester"]["tenant_id"],
        workspace_id=workspace_id or operation["requester"]["workspace_id"],
        expires_at=NOW + timedelta(hours=1),
    )


def _boundary() -> ApprovalIdentityBoundary:
    operation = _operation()
    return ApprovalIdentityBoundary(
        tenant_id=operation["requester"]["tenant_id"],
        workspace_id=operation["requester"]["workspace_id"],
        requester_client_ids=frozenset({operation["requester"]["client_id"]}),
        approver_client_ids=frozenset({operation["requester"]["client_id"]}),
        trusted_audiences=frozenset({"operations-api"}),
    )


class FakeEvidenceStore:
    def __init__(self, evidence: OperationEvidence | None) -> None:
        self._evidence = evidence
        self.calls: list[str] = []

    def load_operation_evidence(self, operation_id: str) -> OperationEvidence | None:
        self.calls.append(operation_id)
        return self._evidence


def _evidence(state: str = "pending_approval", approval: dict | None = None) -> OperationEvidence:
    return OperationEvidence(
        operation=_operation(),
        prepared_hash=_operation()["prepared_hash"],
        state=state,
        approval=approval,
        ledger=[
            {"sequence": 0, "event_type": "operation.prepared"},
        ],
    )


def _service(store: FakeEvidenceStore) -> E2EvidenceService:
    return E2EvidenceService(store=store, identity_boundary=_boundary())


def test_returns_bounded_evidence_for_owning_workspace() -> None:
    operation = _operation()
    store = FakeEvidenceStore(_evidence())
    view = _service(store).load_evidence(operation_id=operation["operation_id"], requester=_principal())
    assert view is not None
    assert view["operation_id"] == operation["operation_id"]
    assert view["state"] == "pending_approval"
    assert view["preview"]["action"] == operation["action"]
    assert view["preview"]["target"] == operation["target"]
    # Identifier-only handoff: the executor binding id, never a credential.
    assert view["handoff"]["executor_id"] == operation["future_executor_binding"]["executor_id"]
    assert "ledger" in view


def test_missing_operation_returns_none() -> None:
    store = FakeEvidenceStore(None)
    view = _service(store).load_evidence(operation_id="op_aaaaaaaaaaaaaaaaaaaaaaaaaa", requester=_principal())
    assert view is None


def test_foreign_workspace_is_invisible() -> None:
    store = FakeEvidenceStore(_evidence())
    operation = _operation()
    view = _service(store).load_evidence(
        operation_id=operation["operation_id"], requester=_principal(workspace_id="workspace.other")
    )
    assert view is None


def test_evidence_carries_no_raw_provider_or_credential_fields() -> None:
    operation = _operation()
    store = FakeEvidenceStore(
        _evidence(state="approved", approval={"approval_id": "approval.x", "decision": "granted"})
    )
    view = _service(store).load_evidence(operation_id=operation["operation_id"], requester=_principal())
    assert view is not None
    serialized = repr(view)
    for forbidden in ("credential", "secret", "SecretAccessKey", "SessionToken", "aws_access_key"):
        assert forbidden not in serialized
    # The approval preview surfaces only the decision + id, not raw internals.
    assert view["approval"]["decision"] == "granted"
