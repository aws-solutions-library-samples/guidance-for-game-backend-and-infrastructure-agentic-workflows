"""CloudWatch control metrics are emitted at each control outcome (issue #416).

The E4 admin control service must emit the bounded control-plane metrics so an
operator can see control activity without reading logs:

* ``control.applied`` — a control change committed and published;
* ``control.version_conflict`` — a compare-and-set conflict (stale/racing write);
* ``control.denied`` — a control change denied on authority (non-admin).

The metrics sink is optional (a ``None`` sink preserves the existing behavior),
dimensionless, and must never break the control flow if it raises.
"""

from __future__ import annotations

# Standard library
import pathlib
from datetime import datetime, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.control_audit_store import ControlCommitOutcome
from operations.control.control_service import ControlServiceError, KillSwitchControlService

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _RecordingMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, name: str, **kwargs: Any) -> None:
        self.events.append(name)


class _Store:
    def __init__(self, outcome: ControlCommitOutcome) -> None:
        self._outcome = outcome

    def current_config_version(self) -> int:
        return 1

    def commit_control_decision(self, **kwargs: Any) -> ControlCommitOutcome:
        return self._outcome


class _Publisher:
    def __init__(self) -> None:
        self.published = 0

    def publish(self, *, document: dict[str, Any], hard_down: bool) -> None:
        self.published += 1


class _Principal:
    def __init__(self, groups: frozenset[str]) -> None:
        self.subject_id = "admin-1"
        self.client_id = "client-1"
        self.groups = groups


def _service(outcome: ControlCommitOutcome, metrics: Any) -> KillSwitchControlService:
    return KillSwitchControlService(
        audit_store=_Store(outcome),
        publisher=_Publisher(),
        admin_group="admin",
        clock=lambda: _NOW,
        metrics=metrics,
    )


def _request(enabled: bool = True) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "expected_config_version": 1,
        "desired": {
            "operations_enabled": enabled,
            "capabilities": {CAPABILITY_ID: {"prepare": enabled, "dispatch": enabled, "execute": enabled}},
        },
    }


def test_applied_emits_control_applied() -> None:
    metrics = _RecordingMetrics()
    service = _service(ControlCommitOutcome.COMMITTED, metrics)
    service.apply(request=_request(), principal=_Principal(frozenset({"admin"})))
    assert "control.applied" in metrics.events


def test_version_conflict_emits_metric() -> None:
    metrics = _RecordingMetrics()
    service = _service(ControlCommitOutcome.VERSION_CONFLICT, metrics)
    service.apply(request=_request(), principal=_Principal(frozenset({"admin"})))
    assert "control.version_conflict" in metrics.events


def test_denied_emits_metric() -> None:
    metrics = _RecordingMetrics()
    service = _service(ControlCommitOutcome.COMMITTED, metrics)
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(), principal=_Principal(frozenset({"users"})))
    assert "control.denied" in metrics.events


def test_metric_failure_never_breaks_control() -> None:
    class _Boom:
        def record(self, name: str, **kwargs: Any) -> None:
            raise RuntimeError("metrics down")

    service = _service(ControlCommitOutcome.COMMITTED, _Boom())
    # A raising metrics sink must not break the committed control change.
    result = service.apply(request=_request(), principal=_Principal(frozenset({"admin"})))
    assert result["outcome"] == "applied"


def test_all_write_entrypoints_wire_the_appconfig_rollback_signal() -> None:
    project_root = pathlib.Path(__file__).parents[3]
    entrypoints = (
        project_root / "backend/src/operations/observe/lambda_entry.py",
        project_root / "backend/src/operations/execute/dispatcher_entry.py",
        project_root / "backend/src/operations/execute/executor_entry.py",
    )
    for entrypoint in entrypoints:
        source = entrypoint.read_text(encoding="utf-8")
        assert "unavailable_callback=" in source, entrypoint
        assert 'record("kill_switch.unavailable")' in source, entrypoint


def test_all_write_entrypoints_wire_the_durable_control_intent_fence() -> None:
    project_root = pathlib.Path(__file__).parents[3]
    entrypoints = (
        project_root / "backend/src/operations/observe/lambda_entry.py",
        project_root / "backend/src/operations/execute/dispatcher_entry.py",
        project_root / "backend/src/operations/execute/executor_entry.py",
    )
    for entrypoint in entrypoints:
        source = entrypoint.read_text(encoding="utf-8")
        assert "DynamoDbDurableControlGate" in source, entrypoint
        assert "durable_control_gate=" in source, entrypoint
