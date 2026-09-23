"""Fresh, separate AppConfig autonomy switch — strict in-code parser (#439, E5).

The bounded-autonomy runtime (track C of issue #439) must never turn on by
accident. Beyond the E4 kill-switch, static deployment mode, and durable control
intent, a **separate** AWS AppConfig configuration document — the *autonomy
switch* — must be freshly present and explicitly enabled before any autonomous
dispatch or provider write may proceed.

Why a separate document, parsed in code
---------------------------------------
* **Separate opt-in.** The E4 kill-switch de-escalates the human-approved E2/E3
  lifecycle. Bounded autonomy is a distinct, higher-authority capability, so it
  gets its own switch that is disabled by default and independent of the E4
  document. Enabling E4 never enables autonomy.
* **No edit to E4's frozen schema.** This document has its own tiny, strict,
  self-contained shape validated by :func:`validate_autonomy_switch_document`
  in code. It does **not** reuse, extend, or re-hash the frozen
  ``operations-kill-switch`` control-plane schema.
* **Fail closed on everything.** Exactly like the E4 gate, this gate reads the
  AppConfig extension fresh on every call (the extension owns poll/cache), never
  caches or falls back, and treats a transport/HTTP error, an empty/malformed
  body, an unknown field, a wrong document/capability version, or a stale
  document (outside ``issued_at``/``not_after``) as :class:`AutonomySwitchUnavailable`.
  The only permit is a fresh, valid document that explicitly enables both the
  primary flag and the exact capability's ``autonomous_write`` flag.
* **No naive timestamp coercion.** ``issued_at``/``not_after`` must carry an
  explicit UTC offset (a normalized ``Z`` is fine). A naive, offset-less
  timestamp is rejected rather than reinterpreted in the host's local zone,
  which would skew the freshness window by the host's UTC offset and could keep
  a stale switch "fresh".

This module holds no credential and performs no provider write. It reads one
localhost AppConfig-extension endpoint (through the same narrow
:class:`~operations.control.kill_switch_gate.ConfigExtensionPort`) and returns a
boolean decision.
"""

from __future__ import annotations

# Standard library
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

# Local modules
from operations.contracts.autonomy import CAPABILITY_ID as _AUTONOMY_CAPABILITY_ID
from operations.control.kill_switch_gate import ConfigExtensionPort

# The autonomy switch document format version. Exact, not a minimum: an unknown
# value fails closed rather than being reinterpreted.
AUTONOMY_SWITCH_DOCUMENT_VERSION = "1.0"

# The bounded-autonomy capability the switch gates. It is the same logical
# capability id as the autonomy contracts (``gamelift.capacity-adjustment``); the
# switch enables its ``autonomous_write`` posture specifically.
AUTONOMY_SWITCH_CAPABILITY_ID = _AUTONOMY_CAPABILITY_ID

# The maximum autonomy-switch body accepted. The document is tiny; anything
# larger is a misconfiguration and is refused rather than read unbounded.
_MAX_BODY_BYTES = 16 * 1024

_REQUIRED_TOP_LEVEL = frozenset(
    {
        "autonomy_switch_version",
        "config_version",
        "issued_at",
        "not_after",
        "autonomy_enabled",
        "capabilities",
    }
)
_REQUIRED_CAPABILITY_KEYS = frozenset({"autonomous_write"})


class AutonomySwitchUnavailable(RuntimeError):
    """The autonomy switch could not be read as a fresh, valid document.

    Raised whenever the extension is unreachable/errored, the body is empty or
    malformed, the document fails its strict in-code contract, or the document is
    stale/not-yet-valid. Every enforcement point treats this as "deny": autonomy
    does not proceed. It never leaks provider or document detail beyond a bounded
    reason string.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("operations autonomy switch is unavailable")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_autonomy_switch_document(document: object) -> None:
    """Validate the autonomy switch document with a tiny strict in-code contract.

    Raises :class:`ValueError` on any deviation: not an object, an unknown or
    missing top-level field, a wrong document version, a non-integer/negative
    ``config_version``, a non-string/empty timestamp, a non-boolean primary flag,
    a non-object ``capabilities`` map, or any capability entry that is not a
    closed ``{"autonomous_write": bool}`` object. This is deliberately separate
    from — and never mutates or re-hashes — E4's frozen control-plane schema.
    """
    if not isinstance(document, dict):
        raise ValueError("autonomy switch document must be a JSON object")

    keys = set(document)
    if keys != _REQUIRED_TOP_LEVEL:
        unknown = sorted(keys - _REQUIRED_TOP_LEVEL)
        missing = sorted(_REQUIRED_TOP_LEVEL - keys)
        detail = []
        if unknown:
            detail.append("unknown fields: " + ", ".join(unknown))
        if missing:
            detail.append("missing fields: " + ", ".join(missing))
        raise ValueError("autonomy switch document has " + "; ".join(detail))

    if document["autonomy_switch_version"] != AUTONOMY_SWITCH_DOCUMENT_VERSION:
        raise ValueError("autonomy switch document version is unsupported")

    if not _is_int(document["config_version"]) or document["config_version"] < 0:
        raise ValueError("config_version must be a non-negative integer")

    for field in ("issued_at", "not_after"):
        value = document[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        # Reject anything that is not a normalized UTC instant we can parse, and
        # reject a naive (offset-less) timestamp rather than assuming UTC.
        _instant(value)

    if not isinstance(document["autonomy_enabled"], bool):
        raise ValueError("autonomy_enabled must be a boolean")

    capabilities = document["capabilities"]
    if not isinstance(capabilities, dict) or not capabilities:
        raise ValueError("capabilities must be a non-empty object")
    for capability_id, block in capabilities.items():
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ValueError("capability id must be a non-empty string")
        if not isinstance(block, dict) or set(block) != _REQUIRED_CAPABILITY_KEYS:
            raise ValueError("capability entry must be exactly {'autonomous_write': bool}")
        if not isinstance(block["autonomous_write"], bool):
            raise ValueError("autonomous_write must be a boolean")


@dataclass(frozen=True, slots=True)
class AutonomySwitchDecision:
    """An immutable snapshot of one fresh autonomy-switch evaluation.

    ``autonomy_allowed`` folds the deployment-wide ``autonomy_enabled`` primary
    flag together with the specific capability's ``autonomous_write`` flag into a
    single boolean. A document that does not carry the gate's capability entry
    cannot enable it.
    """

    autonomy_enabled: bool
    config_version: int
    _capability_flags: dict[str, bool]
    _capability_id: str

    def autonomy_allowed(self) -> bool:
        """Return whether autonomous writes are permitted under this decision."""
        if not self.autonomy_enabled:
            return False
        return bool(self._capability_flags.get(self._capability_id, False))


class AutonomySwitchGate:
    """Read, validate, freshness-check, and gate on the autonomy switch document.

    Constructed over the same narrow :class:`ConfigExtensionPort` the E4 gate
    uses, but pointed (by the caller's settings) at a *separate* AppConfig
    profile. It holds no state between calls and never caches or falls back.
    """

    def __init__(
        self,
        *,
        extension: ConfigExtensionPort,
        capability_id: str = AUTONOMY_SWITCH_CAPABILITY_ID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ValueError("capability_id must be a non-empty string")
        self._extension = extension
        self._capability_id = capability_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(self) -> AutonomySwitchDecision:
        """Read the extension fresh and return an immutable decision, or fail closed."""
        document = self._read_fresh_document()
        capabilities = document["capabilities"]
        flags = {capability_id: bool(block["autonomous_write"]) for capability_id, block in capabilities.items()}
        return AutonomySwitchDecision(
            autonomy_enabled=bool(document["autonomy_enabled"]),
            config_version=int(document["config_version"]),
            _capability_flags=flags,
            _capability_id=self._capability_id,
        )

    def require_enabled(self) -> AutonomySwitchDecision:
        """Return the decision only if autonomy is permitted, else fail closed.

        Every deny path — a disabled primary flag, a disabled/absent capability,
        or an unreadable/invalid/stale document — surfaces as a single
        :class:`AutonomySwitchUnavailable` so a composite gate has exactly one
        deny signal to catch.
        """
        decision = self.evaluate()
        if not decision.autonomy_allowed():
            raise AutonomySwitchUnavailable("autonomy switch does not enable this capability")
        return decision

    # -- Internals -------------------------------------------------------

    def _read_fresh_document(self) -> dict[str, Any]:
        try:
            raw = self._extension.fetch_configuration()
        except AutonomySwitchUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure fails closed
            raise AutonomySwitchUnavailable("extension unavailable") from exc

        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise AutonomySwitchUnavailable("extension returned an empty body")
        if len(raw) > _MAX_BODY_BYTES:
            raise AutonomySwitchUnavailable("autonomy switch body exceeds the maximum size")

        try:
            document = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise AutonomySwitchUnavailable("autonomy switch document is not valid JSON") from exc
        if not isinstance(document, dict):
            raise AutonomySwitchUnavailable("autonomy switch document is not a JSON object")

        try:
            validate_autonomy_switch_document(document)
        except ValueError as exc:
            raise AutonomySwitchUnavailable("autonomy switch document failed its contract") from exc

        self._require_fresh(document)
        return document

    def _require_fresh(self, document: dict[str, Any]) -> None:
        now = self._clock().astimezone(timezone.utc)
        issued_at = _instant(document["issued_at"])
        not_after = _instant(document["not_after"])
        if now < issued_at:
            raise AutonomySwitchUnavailable("autonomy switch document is not yet valid")
        if now >= not_after:
            raise AutonomySwitchUnavailable("autonomy switch document is stale")


def _instant(value: str) -> datetime:
    """Parse a timezone-aware instant, rejecting a naive (offset-less) timestamp.

    ``datetime.astimezone`` on a *naive* value silently reinterprets it in the
    host's local timezone, so a timestamp without an explicit UTC marker would
    skew the freshness window by the host's UTC offset. The switch must never
    guess a timezone: an offset-less timestamp is rejected rather than assumed to
    be UTC. A normalized ``Z`` or an explicit numeric offset is accepted.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("timestamp is not a valid ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must carry an explicit UTC offset")
    return parsed.astimezone(timezone.utc)
