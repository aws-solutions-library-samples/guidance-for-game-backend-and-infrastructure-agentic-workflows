"""Kill-switch gate tests over the AppConfig Lambda extension (issue #416, E4).

The :class:`~operations.control.kill_switch_gate.KillSwitchGate` reads the single
deployment-wide kill-switch document from the local AWS AppConfig Lambda
extension endpoint, validates the exact self-contained contract, requires the
document to be fresh (``issued_at`` <= now < ``not_after``), and fails closed on
every failure mode: the extension unavailable (network/timeout/HTTP error), a
malformed body, a schema/semantic contract violation, or a stale document.

The gate NEVER caches and NEVER falls back to a stored document: every
evaluation reads the extension fresh, so a flipped switch takes effect on the
next request. The gate can only *reduce* the static deployment authority — a
phase the static authority already denies stays denied regardless of the
document, and the document can only turn a statically-permitted phase off.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID, default_safe_kill_switch
from operations.control.kill_switch_gate import (
    KillSwitchGate,
    KillSwitchUnavailable,
    PhaseDenied,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh_document(**overrides: Any) -> dict[str, Any]:
    document = {
        "contract_version": "1.0",
        "config_version": 7,
        "issued_at": _z(_NOW - timedelta(seconds=30)),
        "not_after": _z(_NOW + timedelta(minutes=5)),
        "operations_enabled": True,
        "capabilities": {
            CAPABILITY_ID: {"prepare": True, "dispatch": True, "execute": True},
        },
    }
    document.update(overrides)
    return document


class _StubExtension:
    """A stub AppConfig extension client returning bytes or raising."""

    def __init__(self, *, body: bytes | None = None, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.calls = 0

    def fetch_configuration(self) -> bytes:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._body is not None
        return self._body


def _gate(extension: _StubExtension, *, static_authority: str = "operate") -> KillSwitchGate:
    return KillSwitchGate(
        extension=extension,
        capability_id=CAPABILITY_ID,
        static_authority=static_authority,
        clock=lambda: _NOW,
    )


def _bytes(document: dict[str, Any]) -> bytes:
    # Standard library
    import json

    return json.dumps(document).encode("utf-8")


# -- Happy path -------------------------------------------------------------


def test_fresh_all_enabled_permits_every_phase() -> None:
    gate = _gate(_StubExtension(body=_bytes(_fresh_document())))
    decision = gate.evaluate()
    assert decision.operations_enabled is True
    assert decision.config_version == 7
    for phase in ("prepare", "dispatch", "execute"):
        assert decision.phase_allowed(phase) is True
        gate.require_phase(phase)  # does not raise


def test_evaluate_reads_extension_every_call_no_cache() -> None:
    extension = _StubExtension(body=_bytes(_fresh_document()))
    gate = _gate(extension)
    gate.evaluate()
    gate.evaluate()
    gate.require_phase("execute")
    assert extension.calls == 3


# -- Fail closed: unavailable ----------------------------------------------


def test_network_error_fails_closed() -> None:
    gate = _gate(_StubExtension(error=OSError("connection refused")))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()
    with pytest.raises(PhaseDenied):
        gate.require_phase("prepare")


def test_malformed_body_fails_closed() -> None:
    gate = _gate(_StubExtension(body=b"not json{{"))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


def test_empty_body_fails_closed() -> None:
    gate = _gate(_StubExtension(body=b""))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


# -- Fail closed: invalid contract -----------------------------------------


def test_schema_violation_fails_closed() -> None:
    bad = _fresh_document()
    bad["capabilities"]["gamelift.capacity-adjustment"]["extra"] = True  # unknown field
    gate = _gate(_StubExtension(body=_bytes(bad)))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


def test_unknown_capability_fails_closed() -> None:
    bad = _fresh_document()
    bad["capabilities"]["gamelift.other"] = {"prepare": False, "dispatch": False, "execute": False}
    gate = _gate(_StubExtension(body=_bytes(bad)))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


def test_semantic_phase_order_violation_fails_closed() -> None:
    # execute enabled without dispatch violates prepare>=dispatch>=execute.
    bad = _fresh_document(capabilities={CAPABILITY_ID: {"prepare": True, "dispatch": False, "execute": True}})
    gate = _gate(_StubExtension(body=_bytes(bad)))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


# -- Fail closed: stale -----------------------------------------------------


def test_expired_document_is_stale_fails_closed() -> None:
    stale = _fresh_document(
        issued_at=_z(_NOW - timedelta(minutes=10)),
        not_after=_z(_NOW - timedelta(seconds=1)),
    )
    gate = _gate(_StubExtension(body=_bytes(stale)))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


def test_not_after_equal_now_is_stale() -> None:
    stale = _fresh_document(
        issued_at=_z(_NOW - timedelta(minutes=1)),
        not_after=_z(_NOW),
    )
    gate = _gate(_StubExtension(body=_bytes(stale)))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


def test_not_yet_valid_document_is_stale() -> None:
    future = _fresh_document(
        issued_at=_z(_NOW + timedelta(seconds=1)),
        not_after=_z(_NOW + timedelta(minutes=5)),
    )
    gate = _gate(_StubExtension(body=_bytes(future)))
    with pytest.raises(KillSwitchUnavailable):
        gate.evaluate()


# -- Switch semantics -------------------------------------------------------


def test_operations_disabled_denies_every_phase() -> None:
    document = default_safe_kill_switch(
        config_version=3,
        issued_at=_z(_NOW - timedelta(seconds=10)),
        not_after=_z(_NOW + timedelta(minutes=5)),
    )
    gate = _gate(_StubExtension(body=_bytes(document)))
    decision = gate.evaluate()
    assert decision.operations_enabled is False
    for phase in ("prepare", "dispatch", "execute"):
        assert decision.phase_allowed(phase) is False
        with pytest.raises(PhaseDenied):
            gate.require_phase(phase)


def test_per_phase_disable_denies_only_that_phase() -> None:
    # prepare on, dispatch/execute off (respects ordering).
    document = _fresh_document(capabilities={CAPABILITY_ID: {"prepare": True, "dispatch": False, "execute": False}})
    gate = _gate(_StubExtension(body=_bytes(document)))
    decision = gate.evaluate()
    assert decision.phase_allowed("prepare") is True
    assert decision.phase_allowed("dispatch") is False
    assert decision.phase_allowed("execute") is False
    gate.require_phase("prepare")
    with pytest.raises(PhaseDenied):
        gate.require_phase("dispatch")


# -- Static authority can only be reduced -----------------------------------


def test_static_authority_below_remediate_denies_execute_even_when_switch_on() -> None:
    # Document permits execute, but the static deployment authority is only
    # ``advise``: the gate can only REDUCE authority, never elevate it.
    gate = _gate(_StubExtension(body=_bytes(_fresh_document())), static_authority="advise")
    decision = gate.evaluate()
    assert decision.phase_allowed("prepare") is True
    # advise permits prepare/dispatch (advise-authority acts) but never execute.
    assert decision.phase_allowed("execute") is False
    with pytest.raises(PhaseDenied):
        gate.require_phase("execute")


def test_static_authority_disabled_denies_all_even_when_switch_on() -> None:
    gate = _gate(_StubExtension(body=_bytes(_fresh_document())), static_authority="disabled")
    decision = gate.evaluate()
    for phase in ("prepare", "dispatch", "execute"):
        assert decision.phase_allowed(phase) is False


def test_require_phase_rejects_unknown_phase() -> None:
    gate = _gate(_StubExtension(body=_bytes(_fresh_document())))
    with pytest.raises(ValueError):
        gate.require_phase("nonsense")
