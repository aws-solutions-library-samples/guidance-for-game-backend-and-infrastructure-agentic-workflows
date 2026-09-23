"""Composite bounded-autonomy runtime gate + runtime-only settings (#439, E5).

Track C of issue #439 wires the *runtime* fences around the E5 bounded-autonomy
contracts (issue #438, integration commit dd1141b) **without** creating any
infrastructure, provider IAM, executor code path, UI, or model path, and without
touching v1 human approval or the frozen E4 control schema.

What this module is
-------------------
A pure policy object with two composite decision points that the future durable,
authenticated Step Functions workflow consults. It is **default disabled** and,
crucially, holds **no provider-write permission and no executor credential**: it
returns an allow/deny reading and (for a write) an atomic reservation result.
It never dispatches, never calls the provider, and never reaches the executor —
those remain the job of the durable workflow, which is the only thing that may
invoke the existing narrow executor, and then only with ``operation_id``.

The two gates
-------------
``require_pre_dispatch()`` — checked *before* an autonomous dispatch. Permits
only when EVERY source agrees:

1. autonomy is enabled in :class:`AutonomyRuntimeSettings` (default off), and
2. the static deployment mode is **exactly** ``operate`` (not merely ``>=``), and
3. the fresh, **separate** AppConfig autonomy switch is enabled
   (:class:`~operations.autonomy_switch.AutonomySwitchGate`), and
4. the E4 kill-switch ``dispatch`` phase permits, and
5. the E4 durable control intent permits ``dispatch``.

``require_pre_provider_write(...)`` — checked *immediately before* the single
provider write. It re-checks emergency disablement (the autonomy switch plus the
E4 ``execute`` phase and durable ``execute`` intent), then runs the pure
deterministic evaluator over trusted inputs only, and only if that authorizes
does it **atomically reserve** budget/cooldown/frequency/concurrency/tenant/
workspace/enrollment/policy/observation/state through the injected durable
reservation port. Any denial, or a reservation that is not granted or errors,
fails closed with :class:`AutonomyGateDenied`.

Every source is intersected and every failure denies. A model may request an
evaluation, but nothing here lets model or request-body input authorize, alter
policy/limits, dispatch, or reach the executor: the evaluator and the reservation
receive only server-owned, hash-bound trusted inputs supplied by the caller.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

# Local modules
from operations.autonomy_switch import AutonomySwitchUnavailable

# Runtime-only environment keys (no CloudFormation, no new provider permission).
_AUTONOMY_ENABLED_KEY = "GBAW_OPERATIONS_AUTONOMY_ENABLED"
_OPERATIONS_MODE_KEY = "GBAW_OPERATIONS_MODE"

# The single static deployment mode under which bounded autonomy may run. Exact,
# not a floor: the operate rung is the only one that admits autonomous writes.
_REQUIRED_STATIC_MODE = "operate"

_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


class AutonomyGateDenied(RuntimeError):
    """A composite autonomy gate denied the requested phase (fail closed).

    The single bounded deny signal both composite gates raise. It carries a short
    machine reason (``phase`` and ``reason``) and never leaks provider, document,
    or credential detail. Any underlying cause (a switch/kill-switch/durable
    denial, a deterministic ``denied`` decision, or a reservation failure) is
    chained via ``__cause__``.
    """

    def __init__(self, phase: str, reason: str) -> None:
        self.phase = phase
        self.reason = reason
        super().__init__(f"autonomy phase '{phase}' is denied")


def _resolve_bool(source: Mapping[str, str], key: str) -> bool:
    raw = source.get(key)
    if raw is None or not raw.strip():
        return False
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"{key} must be a boolean token")


@dataclass(frozen=True, slots=True)
class AutonomyRuntimeSettings:
    """Runtime-only resolved settings for bounded autonomy. Default disabled.

    ``enabled`` gates the whole feature; it defaults to ``False`` and can only be
    ``True`` when the static deployment mode is exactly ``operate``. No provider
    permission, ARN, or infrastructure identifier lives here — this is a runtime
    lever only.
    """

    enabled: bool
    static_deployment_mode: str

    def __post_init__(self) -> None:
        if not isinstance(self.static_deployment_mode, str) or not self.static_deployment_mode.strip():
            raise ValueError("static_deployment_mode must be a non-empty string")
        if self.enabled and self.static_deployment_mode != _REQUIRED_STATIC_MODE:
            raise ValueError("bounded autonomy requires the static deployment mode to be exactly 'operate'")


def resolve_autonomy_runtime_settings(env: Mapping[str, str] | None = None) -> AutonomyRuntimeSettings:
    """Resolve runtime-only autonomy settings, defaulting to disabled.

    Autonomy is disabled unless ``GBAW_OPERATIONS_AUTONOMY_ENABLED`` is a true
    token AND ``GBAW_OPERATIONS_MODE`` is exactly ``operate``. Any other
    combination either resolves to disabled (flag off/unset) or fails closed with
    :class:`ValueError` (flag on but mode not operate). The autonomy flag is
    deliberately independent of, and additive to, the existing operations mode.
    """
    # Standard library
    import os

    source: Mapping[str, str] = os.environ if env is None else env
    static_mode = (source.get(_OPERATIONS_MODE_KEY) or "disabled").strip().lower() or "disabled"
    requested = _resolve_bool(source, _AUTONOMY_ENABLED_KEY)
    if not requested:
        return AutonomyRuntimeSettings(enabled=False, static_deployment_mode=static_mode)
    # Requested on: __post_init__ enforces the exact-operate rule, failing closed.
    return AutonomyRuntimeSettings(enabled=True, static_deployment_mode=static_mode)


class _SwitchGatePort(Protocol):
    def require_enabled(self) -> Any: ...


class _PhaseGatePort(Protocol):
    def require_phase(self, phase: str) -> Any: ...


class _DurableGatePort(Protocol):
    def require_phase(self, phase: str, *, deployed_decision: Any) -> None: ...


class ReservationPort(Protocol):
    """Atomic durable reservation of every rolling-window guardrail.

    The implementation (owned by the durable workflow, not by this gate) performs
    a single conditional/transactional durable reservation that atomically debits
    budget and increments cooldown/frequency/concurrency counters, pinned to the
    exact tenant/workspace/enrollment/policy/observation/state revision. It
    returns ``True`` only if the reservation was granted, ``False`` if a
    conditional check failed (already reserved, revision moved, limit reached),
    and raises on any provider error. Every non-grant fails the gate closed.
    """

    def reserve(self, **kwargs: Any) -> bool: ...


class AutonomyRuntimeGate:
    """Compose the runtime autonomy fences around the pure E5 evaluator."""

    def __init__(
        self,
        *,
        settings: AutonomyRuntimeSettings,
        switch_gate: _SwitchGatePort,
        kill_switch_gate: _PhaseGatePort,
        durable_control_gate: _DurableGatePort,
        evaluator: Callable[..., dict[str, Any]],
        reservation_port: ReservationPort,
    ) -> None:
        self._settings = settings
        self._switch_gate = switch_gate
        self._kill_switch_gate = kill_switch_gate
        self._durable_control_gate = durable_control_gate
        self._evaluator = evaluator
        self._reservation_port = reservation_port

    # -- Composite gate 1: pre-dispatch ----------------------------------

    def require_pre_dispatch(self) -> None:
        """Permit an autonomous dispatch only if every source agrees, else deny."""
        self._require_static_authority("dispatch")
        self._require_emergency_enabled("dispatch")

    # -- Composite gate 2: immediate pre-provider-write ------------------

    def require_pre_provider_write(
        self,
        *,
        policy: dict[str, Any],
        authority_inputs: dict[str, str],
        automation_principal: dict[str, str],
        observation: dict[str, Any],
        requested: dict[str, int],
        window_state: dict[str, Any],
        now_epoch_seconds: int,
    ) -> dict[str, Any]:
        """Authorize and atomically reserve immediately before the single write.

        Order is deliberate and fail-closed:

        1. static authority (exactly ``operate``) and enablement,
        2. emergency disablement re-check at the ``execute`` phase,
        3. the pure deterministic evaluator over trusted inputs only,
        4. atomic durable reservation of all rolling-window guardrails.

        Returns the deterministic decision reading when authorized and reserved.
        The gate performs no provider write; the caller (the durable workflow)
        does, using ``operation_id`` alone.
        """
        self._require_static_authority("execute")
        # Emergency disablement is checked immediately before the provider write.
        self._require_emergency_enabled("execute")

        decision = self._evaluator(
            policy=policy,
            authority_inputs=authority_inputs,
            automation_principal=automation_principal,
            observation=observation,
            requested=requested,
            window_state=window_state,
            now_epoch_seconds=now_epoch_seconds,
        )
        if not isinstance(decision, dict) or decision.get("decision") != "authorized":
            raise AutonomyGateDenied("execute", "deterministic decision denied")

        # Atomic reservation of budget/cooldown/frequency/concurrency and the
        # tenant/workspace/enrollment/policy/observation/state pins. Any non-grant
        # or provider error fails closed; nothing is written by this gate.
        try:
            reserved = self._reservation_port.reserve(
                policy=policy,
                observation=observation,
                requested=requested,
                window_state=window_state,
                now_epoch_seconds=now_epoch_seconds,
                decision=decision,
            )
        except AutonomyGateDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - any reservation error fails closed
            raise AutonomyGateDenied("execute", "reservation unavailable") from exc
        if reserved is not True:
            raise AutonomyGateDenied("execute", "reservation not granted")

        return decision

    # -- Internals -------------------------------------------------------

    def _require_static_authority(self, phase: str) -> None:
        if not self._settings.enabled:
            raise AutonomyGateDenied(phase, "autonomy disabled")
        if self._settings.static_deployment_mode != _REQUIRED_STATIC_MODE:
            raise AutonomyGateDenied(phase, "static deployment mode is not exactly operate")

    def _require_emergency_enabled(self, phase: str) -> None:
        """Intersect the separate autonomy switch with the E4 phase + durable intent.

        ``phase`` is the E4 lifecycle phase used for the kill-switch/durable
        check (``dispatch`` pre-dispatch, ``execute`` immediately before write).
        Any unavailable/denied source fails closed as one bounded denial.
        """
        try:
            self._switch_gate.require_enabled()
        except AutonomySwitchUnavailable as exc:
            raise AutonomyGateDenied(phase, "autonomy switch disabled or unavailable") from exc
        except Exception as exc:  # noqa: BLE001 - any switch failure fails closed
            raise AutonomyGateDenied(phase, "autonomy switch disabled or unavailable") from exc

        try:
            decision = self._kill_switch_gate.require_phase(phase)
            self._durable_control_gate.require_phase(phase, deployed_decision=decision)
        except Exception as exc:  # noqa: BLE001 - any E4 denial fails closed
            raise AutonomyGateDenied(phase, "phase disabled by kill-switch or durable intent") from exc
