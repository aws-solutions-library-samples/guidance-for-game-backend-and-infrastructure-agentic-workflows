"""E3 Lambda entry wiring tests (#415).

These assert the deployable dispatcher and executor entries are import-safe and
that their thin store adapters correctly reduce an E2 OperationEvidence into the
minimal dispatch view / reload pair the E3 boundary needs — without any AWS call.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import capacity_prepared_hash, load_json
from operations.evidence import OperationEvidence
from operations.execute.dispatcher_entry import EvidenceDispatchStore
from operations.execute.executor_entry import EvidenceExecutionReloadStore
from operations.playbook_definition import capacity_playbook_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"


def _prepared() -> dict[str, Any]:
    prepared = load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")
    prepared["playbook"]["playbook_hash"] = capacity_playbook_hash()
    prepared["prepared_hash"] = capacity_prepared_hash(prepared)
    return prepared


class _FakeEvidenceStore:
    def __init__(self, evidence: OperationEvidence | None) -> None:
        self._evidence = evidence

    def load_operation_evidence(self, operation_id: str) -> OperationEvidence | None:
        return self._evidence


def _evidence(state: str, approval: dict[str, Any] | None = None) -> OperationEvidence:
    prepared = _prepared()
    return OperationEvidence(
        operation=prepared,
        prepared_hash=prepared["prepared_hash"],
        state=state,
        approval=approval,
        ledger=[],
    )


def test_entries_import_safely() -> None:
    # Importing must not resolve settings or build AWS clients at import time.
    # Local modules
    from operations.execute import dispatcher_entry, executor_entry

    assert hasattr(dispatcher_entry, "handler")
    assert hasattr(executor_entry, "handler")


def test_dispatch_view_projects_state_and_scope() -> None:
    evidence = _evidence("approved")
    store = EvidenceDispatchStore(_FakeEvidenceStore(evidence))
    view = store.load_dispatch_view("op_aaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert view is not None
    assert view["state"] == "approved"
    assert view["tenant_id"] == evidence.operation["requester"]["tenant_id"]
    assert view["workspace_id"] == evidence.operation["requester"]["workspace_id"]


def test_dispatch_view_none_when_missing() -> None:
    store = EvidenceDispatchStore(_FakeEvidenceStore(None))
    assert store.load_dispatch_view("op_aaaaaaaaaaaaaaaaaaaaaaaaaa") is None


def test_reload_store_returns_full_operation_and_approval() -> None:
    approval = {"decision": "granted", "operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}
    evidence = _evidence("approved", approval=approval)
    store = EvidenceExecutionReloadStore(_FakeEvidenceStore(evidence))
    reloaded = store.load_for_execution("op_aaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert reloaded is not None
    prepared_operation, loaded_approval, state = reloaded
    assert prepared_operation == evidence.operation
    assert loaded_approval == approval
    assert state == "approved"


def test_reload_store_none_when_no_approval() -> None:
    evidence = _evidence("approved", approval=None)
    store = EvidenceExecutionReloadStore(_FakeEvidenceStore(evidence))
    assert store.load_for_execution("op_aaaaaaaaaaaaaaaaaaaaaaaaaa") is None
