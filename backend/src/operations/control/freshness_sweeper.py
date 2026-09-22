"""System-only deadman refresh for the E4 AppConfig kill switch (#416).

A kill-switch document is accepted only until its immutable ``not_after``
instant. This service keeps an actively managed deployment fresh without
changing authority: it reads one schema-valid document from the AppConfig
extension, and when the refresh window is reached it submits the exact same
booleans through the audited CAS control service. The control service advances
the version, records the system actor, and uses the normal gradual strategy.

The system principal is created only inside this module. It is not request-
deserializable, carries no credential, and can only replay the booleans already
present in the validated deployed document. An unavailable or malformed
document fails closed. A concurrent admin/refresh conflict is a safe no-op.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID, CONTROL_PHASES


class ValidDocumentGatePort(Protocol):
    def read_valid_document(self) -> dict[str, Any]: ...


class ControlServicePort(Protocol):
    def apply(
        self,
        *,
        request: dict[str, Any],
        principal: Any,
        current_document: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FreshnessRefreshResult:
    attempted: bool
    applied: bool
    outcome: str
    config_version: int


@dataclass(frozen=True, slots=True)
class _SystemPrincipal:
    subject_id: str
    client_id: str
    groups: frozenset[str]


class KillSwitchFreshnessService:
    """Refresh a due validated document without changing its authority flags."""

    def __init__(
        self,
        *,
        gate: ValidDocumentGatePort,
        control_service: ControlServicePort,
        admin_group: str,
        refresh_before_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(admin_group, str) or not admin_group.strip():
            raise ValueError("admin_group must be a non-empty string")
        if refresh_before_seconds <= 0:
            raise ValueError("refresh_before_seconds must be positive")
        self._gate = gate
        self._control_service = control_service
        self._admin_group = admin_group
        self._refresh_before_seconds = int(refresh_before_seconds)
        self._clock = clock

    def run(self) -> FreshnessRefreshResult:
        document = self._gate.read_valid_document()
        config_version = int(document["config_version"])
        now = self._clock().astimezone(timezone.utc)
        not_after = _instant(document["not_after"])
        remaining = (not_after - now).total_seconds()
        if remaining > self._refresh_before_seconds:
            return FreshnessRefreshResult(
                attempted=False,
                applied=False,
                outcome="not_due",
                config_version=config_version,
            )

        desired = {
            "operations_enabled": bool(document["operations_enabled"]),
            "capabilities": {
                CAPABILITY_ID: {phase: bool(document["capabilities"][CAPABILITY_ID][phase]) for phase in CONTROL_PHASES}
            },
        }
        request = {
            "contract_version": "1.0",
            "expected_config_version": config_version,
            "desired": desired,
        }
        principal = _SystemPrincipal(
            subject_id="operations.freshness-sweeper",
            client_id="operations.control-plane",
            groups=frozenset({self._admin_group}),
        )
        response = self._control_service.apply(
            request=request,
            principal=principal,
            current_document=document,
        )
        outcome = response.get("outcome") if isinstance(response, dict) else None
        resulting_version = response.get("config_version") if isinstance(response, dict) else None
        if not isinstance(outcome, str) or not isinstance(resulting_version, int):
            raise RuntimeError("freshness refresh returned an invalid control outcome")
        return FreshnessRefreshResult(
            attempted=True,
            applied=outcome == "applied",
            outcome=outcome,
            config_version=resulting_version,
        )


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("kill-switch freshness timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("kill-switch freshness timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("kill-switch freshness timestamp is invalid")
    return parsed.astimezone(timezone.utc)
