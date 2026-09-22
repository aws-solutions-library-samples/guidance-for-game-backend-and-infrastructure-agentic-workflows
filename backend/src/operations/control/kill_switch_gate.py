"""Fail-closed kill-switch gate over the AWS AppConfig Lambda extension (#416).

The kill-switch is the single deployment-wide runtime lever for the E4 control
plane. It is published as one AWS AppConfig configuration document and read on
the request path through the **AppConfig Lambda extension**, which the extension
exposes as a localhost HTTP endpoint inside the function's execution
environment. :class:`KillSwitchGate` is the thin, protocol-neutral policy object
every enforcement point (prepare, dispatch, and the executor — twice) uses to
decide whether a lifecycle phase may proceed.

Design contract
---------------
* **No custom cache, no fallback.** The gate never caches a document and never
  substitutes a stored/last-known-good document. Every :meth:`evaluate` reads the
  extension fresh (the extension itself owns the poll/cache lifecycle), so a
  flipped switch takes effect on the very next request. The gate holds no state
  between calls.
* **Exact self-contained schema.** The fetched bytes are parsed as JSON and
  validated against the immutable ``operations-kill-switch`` contract (schema +
  semantic invariants) via :func:`validate_control_contract`. The document is a
  closed object with no unknown capability or field.
* **Freshness required.** The document must satisfy ``issued_at <= now <
  not_after``. A document at or past ``not_after``, or one not yet valid, is
  treated as stale and fails closed.
* **Fail closed on everything.** A network/timeout/HTTP error from the
  extension, an empty or malformed body, a schema/semantic violation, or a stale
  document all raise :class:`KillSwitchUnavailable` from :meth:`evaluate`. There
  is no "assume enabled" branch: the only way a phase is permitted is a fresh,
  valid document that explicitly enables it. :meth:`require_phase` reduces every
  denial — including an unavailable/invalid/stale document — to a single
  :class:`PhaseDenied` so an enforcement point has exactly one deny signal.
* **Only reduces static authority.** The gate is constructed with the
  deployment's already-resolved *static* authority (the effective deployment
  mode/ceiling). A phase is permitted only when BOTH the static authority admits
  it AND the kill-switch document enables it. The document can turn a
  statically-permitted phase off; it can never turn a statically-denied phase
  on. This makes the kill-switch a pure de-escalation lever.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

# Local modules
from operations.contracts.control_plane import (
    CONTROL_PHASES,
    KILL_SWITCH_SCHEMA_NAME,
    ControlContractError,
    validate_control_contract,
)

# The minimum static authority (ADR 0001 lattice) each lifecycle phase requires.
# prepare and dispatch are advise-authority acts (no provider write); execute is
# the remediate-authority provider write. The gate can only reduce below these.
_AUTHORITY_ORDER = {"disabled": 0, "observe": 1, "advise": 2, "remediate": 3, "operate": 4}
_PHASE_MINIMUM_AUTHORITY = {
    "prepare": "advise",
    "dispatch": "advise",
    "execute": "remediate",
}


class KillSwitchUnavailable(RuntimeError):
    """The kill-switch could not be read as a fresh, valid document (fail closed).

    Raised by :meth:`KillSwitchGate.evaluate` when the AppConfig extension is
    unreachable or errored, the body is empty or malformed, the document fails
    its schema/semantic contract, or the document is stale (outside
    ``issued_at``/``not_after``). Every enforcement point treats this as "deny":
    the phase does not proceed.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("operations kill-switch is unavailable")


class PhaseDenied(RuntimeError):
    """A lifecycle phase is not permitted (fail closed).

    Raised by :meth:`KillSwitchGate.require_phase` when the phase is disabled by
    the fresh document, denied by the static deployment authority, OR when no
    fresh valid document could be read at all. It is the single bounded deny
    signal an enforcement point handles; it never leaks the document or provider
    detail. When the underlying cause was an unreadable switch, the originating
    :class:`KillSwitchUnavailable` is chained (``__cause__``).
    """

    def __init__(self, phase: str, reason: str) -> None:
        self.phase = phase
        self.reason = reason
        super().__init__(f"operations phase '{phase}' is denied")


class ConfigExtensionPort(Protocol):
    """The narrow port that reads raw kill-switch bytes from the extension.

    The single method returns the raw configuration bytes the AppConfig Lambda
    extension currently serves, or raises on any transport/HTTP failure. It
    performs no parsing or validation — that is the gate's job.
    """

    def fetch_configuration(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    """An immutable snapshot of one fresh kill-switch evaluation.

    ``phase_allowed`` folds together the document's per-phase enablement, the
    deployment-wide ``operations_enabled`` master flag, and the static authority
    floor, so a caller reads a single boolean per phase.
    """

    operations_enabled: bool
    config_version: int
    _phase_document_flags: dict[str, bool]
    _static_authority: str

    def phase_allowed(self, phase: str) -> bool:
        """Return whether ``phase`` may proceed under this decision."""
        if phase not in _PHASE_MINIMUM_AUTHORITY:
            raise ValueError(f"unknown lifecycle phase: {phase}")
        if not self.operations_enabled:
            return False
        if not self._phase_document_flags.get(phase, False):
            return False
        required = _PHASE_MINIMUM_AUTHORITY[phase]
        return _AUTHORITY_ORDER[self._static_authority] >= _AUTHORITY_ORDER[required]


class KillSwitchGate:
    """Read, validate, freshness-check, and gate on the kill-switch document."""

    def __init__(
        self,
        *,
        extension: ConfigExtensionPort,
        capability_id: str,
        static_authority: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ValueError("capability_id must be a non-empty string")
        if static_authority not in _AUTHORITY_ORDER:
            raise ValueError("static_authority must be a valid ADR 0001 authority")
        self._extension = extension
        self._capability_id = capability_id
        self._static_authority = static_authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(self) -> KillSwitchDecision:
        """Read the extension fresh and return an immutable decision, or fail closed."""
        document = self._load_fresh_document()
        switches = document["capabilities"][self._capability_id]
        return KillSwitchDecision(
            operations_enabled=bool(document["operations_enabled"]),
            config_version=int(document["config_version"]),
            _phase_document_flags={phase: bool(switches[phase]) for phase in CONTROL_PHASES},
            _static_authority=self._static_authority,
        )

    def require_phase(self, phase: str) -> KillSwitchDecision:
        """Evaluate and return the decision, or raise :class:`PhaseDenied`.

        Every deny path — a disabled/denied phase or an unavailable/invalid/stale
        document — surfaces as a single :class:`PhaseDenied`, so an enforcement
        point has exactly one signal to catch. The decision is returned only when
        the phase is permitted, so a caller can record the observed
        ``config_version``.
        """
        if phase not in _PHASE_MINIMUM_AUTHORITY:
            raise ValueError(f"unknown lifecycle phase: {phase}")
        try:
            decision = self.evaluate()
        except KillSwitchUnavailable as exc:
            # Fail closed: an unreadable/invalid/stale switch denies the phase.
            raise PhaseDenied(phase, "kill-switch unavailable") from exc
        if not decision.phase_allowed(phase):
            raise PhaseDenied(phase, "phase disabled by kill-switch or static authority")
        return decision

    # -- Internals -------------------------------------------------------

    def _load_fresh_document(self) -> dict[str, Any]:
        try:
            raw = self._extension.fetch_configuration()
        except KillSwitchUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure fails closed
            raise KillSwitchUnavailable("extension unavailable") from exc

        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise KillSwitchUnavailable("extension returned an empty body")

        # Standard library
        import json

        try:
            document = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise KillSwitchUnavailable("kill-switch document is not valid JSON") from exc
        if not isinstance(document, dict):
            raise KillSwitchUnavailable("kill-switch document is not a JSON object")

        try:
            validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)
        except ControlContractError as exc:
            raise KillSwitchUnavailable("kill-switch document failed its contract") from exc

        self._require_fresh(document)
        return document

    def _require_fresh(self, document: dict[str, Any]) -> None:
        now = self._clock().astimezone(timezone.utc)
        issued_at = _instant(document["issued_at"])
        not_after = _instant(document["not_after"])
        if now < issued_at:
            raise KillSwitchUnavailable("kill-switch document is not yet valid")
        if now >= not_after:
            raise KillSwitchUnavailable("kill-switch document is stale")


def _instant(value: str) -> datetime:
    """Parse a normalized UTC ``Z`` timestamp into a timezone-aware instant."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
