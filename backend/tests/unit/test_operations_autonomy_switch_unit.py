"""Strict fail-closed parser for the separate E5 autonomy AppConfig switch (#439).

The autonomy switch is a SEPARATE AppConfig configuration document from the E4
kill-switch. It is the fresh, standalone opt-in that must be explicitly enabled
before any bounded-autonomy dispatch or provider write may proceed. It is parsed
and validated entirely in code by a tiny strict validator; E4's frozen control
schema is not touched or reused.

Every deny path is fail-closed: a missing/malformed/stale/unknown-field/
wrong-capability/wrong-version/disabled document denies. The only way it permits
is a fresh, valid document that explicitly enables the exact capability.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_switch import (
    AUTONOMY_SWITCH_CAPABILITY_ID,
    AUTONOMY_SWITCH_DOCUMENT_VERSION,
    AutonomySwitchDecision,
    AutonomySwitchGate,
    AutonomySwitchUnavailable,
    validate_autonomy_switch_document,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _document(
    *,
    enabled: bool = True,
    config_version: int = 3,
    capability_enabled: bool = True,
    issued_delta: timedelta = timedelta(minutes=-1),
    not_after_delta: timedelta = timedelta(hours=1),
    document_version: str = AUTONOMY_SWITCH_DOCUMENT_VERSION,
    capability_id: str = AUTONOMY_SWITCH_CAPABILITY_ID,
) -> dict[str, Any]:
    return {
        "autonomy_switch_version": document_version,
        "config_version": config_version,
        "issued_at": _iso(_NOW + issued_delta),
        "not_after": _iso(_NOW + not_after_delta),
        "autonomy_enabled": enabled,
        "capabilities": {capability_id: {"autonomous_write": capability_enabled}},
    }


class _Extension:
    """A fake ConfigExtensionPort serving raw bytes or raising."""

    def __init__(self, raw: bytes | Exception) -> None:
        self._raw = raw
        self.calls = 0

    def fetch_configuration(self) -> bytes:
        self.calls += 1
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


def _gate(document_or_raw: Any, *, clock: datetime = _NOW) -> AutonomySwitchGate:
    if isinstance(document_or_raw, (bytes, bytearray, Exception)):
        raw = document_or_raw
    else:
        raw = _bytes(document_or_raw)
    return AutonomySwitchGate(
        extension=_Extension(raw),
        capability_id=AUTONOMY_SWITCH_CAPABILITY_ID,
        clock=lambda: clock,
    )


# -- Pure validator -------------------------------------------------------


def test_valid_document_validates() -> None:
    validate_autonomy_switch_document(_document())


def test_unknown_top_level_field_denies() -> None:
    doc = _document()
    doc["surprise"] = True
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(doc)


def test_wrong_document_version_denies() -> None:
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(_document(document_version="9.9"))


def test_missing_field_denies() -> None:
    doc = _document()
    del doc["autonomy_enabled"]
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(doc)


def test_non_bool_enabled_denies() -> None:
    doc = _document()
    doc["autonomy_enabled"] = "true"
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(doc)


def test_capability_block_unknown_field_denies() -> None:
    doc = _document()
    doc["capabilities"][AUTONOMY_SWITCH_CAPABILITY_ID]["extra"] = True
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(doc)


def test_negative_config_version_denies() -> None:
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(_document(config_version=-1))


def test_bool_config_version_denies() -> None:
    doc = _document()
    doc["config_version"] = True
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(doc)


# -- Gate: transport / freshness / fail-closed ----------------------------


def test_gate_permits_fresh_enabled_document() -> None:
    gate = _gate(_document(enabled=True, capability_enabled=True, config_version=5))
    decision = gate.evaluate()
    assert isinstance(decision, AutonomySwitchDecision)
    assert decision.autonomy_allowed() is True
    assert decision.config_version == 5


def test_gate_denies_when_master_disabled() -> None:
    gate = _gate(_document(enabled=False, capability_enabled=True))
    assert gate.evaluate().autonomy_allowed() is False


def test_gate_denies_when_capability_disabled() -> None:
    gate = _gate(_document(enabled=True, capability_enabled=False))
    assert gate.evaluate().autonomy_allowed() is False


def test_gate_fails_closed_on_transport_error() -> None:
    gate = _gate(RuntimeError("boom"))
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_fails_closed_on_empty_body() -> None:
    gate = _gate(b"")
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_fails_closed_on_malformed_json() -> None:
    gate = _gate(b"{not json")
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_fails_closed_on_non_object() -> None:
    gate = _gate(b"[1,2,3]")
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_fails_closed_on_contract_violation() -> None:
    gate = _gate(_document(document_version="0.0"))
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_fails_closed_when_not_yet_valid() -> None:
    gate = _gate(_document(issued_delta=timedelta(minutes=5)))
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_fails_closed_when_stale() -> None:
    gate = _gate(_document(not_after_delta=timedelta(minutes=-1)))
    with pytest.raises(AutonomySwitchUnavailable):
        gate.evaluate()


def test_gate_denies_missing_capability_entry() -> None:
    doc = _document()
    doc["capabilities"] = {"gamelift.other": {"autonomous_write": True}}
    gate = _gate(doc)
    # A document that does not carry our capability entry cannot enable it.
    assert gate.evaluate().autonomy_allowed() is False


def test_require_enabled_raises_when_denied() -> None:
    gate = _gate(_document(enabled=False))
    with pytest.raises(AutonomySwitchUnavailable):
        gate.require_enabled()


def test_require_enabled_returns_decision_when_permitted() -> None:
    gate = _gate(_document(enabled=True, capability_enabled=True, config_version=9))
    decision = gate.require_enabled()
    assert decision.config_version == 9
