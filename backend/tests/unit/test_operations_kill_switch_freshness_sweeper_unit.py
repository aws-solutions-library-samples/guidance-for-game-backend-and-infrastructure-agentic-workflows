"""Deadman freshness refresh for the E4 AppConfig kill switch (#416)."""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.freshness_sweeper import KillSwitchFreshnessService

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _document(*, version: int = 7, remaining_seconds: int = 600, stale: bool = False) -> dict[str, Any]:
    issued_at = _NOW - timedelta(hours=2 if stale else 1)
    not_after = _NOW - timedelta(seconds=1) if stale else _NOW + timedelta(seconds=remaining_seconds)
    return {
        "contract_version": "1.0",
        "config_version": version,
        "issued_at": _z(issued_at),
        "not_after": _z(not_after),
        "operations_enabled": True,
        "capabilities": {CAPABILITY_ID: {"prepare": True, "dispatch": False, "execute": False}},
    }


class _Gate:
    def __init__(self, document: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.document = document
        self.error = error
        self.calls = 0

    def read_valid_document(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.document is not None
        return self.document


class _Control:
    def __init__(self, outcome: str = "applied") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def apply(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        request = kwargs["request"]
        version = request["expected_config_version"] + (1 if self.outcome == "applied" else 0)
        return {
            "contract_version": "1.0",
            "outcome": self.outcome,
            "config_version": version,
            "reason_code": "APPLIED" if self.outcome == "applied" else "VERSION_CONFLICT",
            **({"effective": kwargs["current_document"]} if self.outcome == "applied" else {}),
        }


def _service(gate: _Gate, control: _Control) -> KillSwitchFreshnessService:
    return KillSwitchFreshnessService(
        gate=gate,
        control_service=control,
        admin_group="admin",
        refresh_before_seconds=1800,
        clock=lambda: _NOW,
    )


def test_fresh_document_outside_refresh_window_is_a_noop() -> None:
    gate = _Gate(_document(remaining_seconds=1801))
    control = _Control()
    result = _service(gate, control).run()
    assert result.attempted is False
    assert result.applied is False
    assert control.calls == []


def test_due_document_is_refreshed_without_changing_authority() -> None:
    document = _document(remaining_seconds=1800)
    gate = _Gate(document)
    control = _Control()
    result = _service(gate, control).run()
    assert result.attempted is True
    assert result.applied is True
    call = control.calls[0]
    assert call["request"] == {
        "contract_version": "1.0",
        "expected_config_version": 7,
        "desired": {
            "operations_enabled": True,
            "capabilities": {CAPABILITY_ID: {"prepare": True, "dispatch": False, "execute": False}},
        },
    }
    assert call["current_document"] == document
    assert call["principal"].groups == frozenset({"admin"})
    assert call["principal"].subject_id == "operations.freshness-sweeper"


def test_schema_valid_stale_document_can_be_safely_refreshed() -> None:
    gate = _Gate(_document(stale=True))
    control = _Control()
    result = _service(gate, control).run()
    assert result.applied is True
    assert len(control.calls) == 1


def test_concurrent_refresh_conflict_is_a_safe_noop() -> None:
    gate = _Gate(_document(remaining_seconds=10))
    control = _Control(outcome="version_conflict")
    result = _service(gate, control).run()
    assert result.attempted is True
    assert result.applied is False
    assert result.outcome == "version_conflict"


def test_unavailable_document_fails_closed() -> None:
    service = _service(_Gate(error=RuntimeError("extension unavailable")), _Control())
    with pytest.raises(RuntimeError):
        service.run()
