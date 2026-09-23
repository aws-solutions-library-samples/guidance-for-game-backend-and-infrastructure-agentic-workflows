"""Executor requires BOTH dispatch audit records and their equality (#439, E5).

Second E5 blocker (executor side): the executor consulted only the ``dispatched``
record. A ``dispatched`` marker without a matching ``dispatch_requested``
predecessor — or one whose operation/execution name disagrees — must fail closed.
``_require_dispatched_audit`` now loads and validates BOTH the
``dispatch_requested`` and ``dispatched`` records and requires they agree on
operation id and execution name before any verify or provider write.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.executor_entry import _require_dispatched_audit
from operations.execute.executor_service import ExecutorServiceError

_OP = "op_" + "e" * 26
_NAME = "op_" + "e" * 26
_OTHER = "op_" + "f" * 26


class _AuditStore:
    def __init__(self, records: dict[str, dict[str, Any] | None]) -> None:
        self._records = records
        self.loads: list[str] = []

    def load_dispatch_audit(self, *, operation_id: str, phase: str) -> dict[str, Any] | None:
        self.loads.append(phase)
        return self._records.get(phase)


def _rec(phase: str, *, operation_id: str = _OP, name: str = _NAME) -> dict[str, Any]:
    return {"operation_id": operation_id, "phase": phase, "execution_name": name}


@pytest.mark.unit
def test_both_records_present_and_matching_passes() -> None:
    store = _AuditStore({"dispatch_requested": _rec("dispatch_requested"), "dispatched": _rec("dispatched")})
    _require_dispatched_audit(store, _OP)
    assert set(store.loads) == {"dispatch_requested", "dispatched"}


@pytest.mark.unit
def test_missing_requested_record_fails_closed() -> None:
    store = _AuditStore({"dispatch_requested": None, "dispatched": _rec("dispatched")})
    with pytest.raises(ExecutorServiceError):
        _require_dispatched_audit(store, _OP)


@pytest.mark.unit
def test_missing_dispatched_record_fails_closed() -> None:
    store = _AuditStore({"dispatch_requested": _rec("dispatch_requested"), "dispatched": None})
    with pytest.raises(ExecutorServiceError):
        _require_dispatched_audit(store, _OP)


@pytest.mark.unit
def test_execution_name_mismatch_fails_closed() -> None:
    store = _AuditStore(
        {
            "dispatch_requested": _rec("dispatch_requested", name=_NAME),
            "dispatched": _rec("dispatched", name=_OTHER),
        }
    )
    with pytest.raises(ExecutorServiceError):
        _require_dispatched_audit(store, _OP)


@pytest.mark.unit
def test_operation_id_mismatch_fails_closed() -> None:
    store = _AuditStore(
        {
            "dispatch_requested": _rec("dispatch_requested", operation_id=_OP),
            "dispatched": _rec("dispatched", operation_id=_OTHER),
        }
    )
    with pytest.raises(ExecutorServiceError):
        _require_dispatched_audit(store, _OP)
