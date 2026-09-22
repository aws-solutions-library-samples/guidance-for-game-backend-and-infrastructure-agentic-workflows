"""Admin kill-switch control service (issue #416, E4).

:class:`KillSwitchControlService` is the protocol-neutral core of the admin
control plane. It takes a verified admin principal and a control request that
carries ONLY desired booleans plus the ``expected_config_version`` (never
identity, credential, or policy) and turns it into an atomic, audited,
compare-and-set kill-switch change.

Flow
----
#. **Authorize.** The acting principal must carry the server-owned admin group;
   a non-admin is denied (fail closed) before anything is read or written.
#. **Validate.** The request is validated against the immutable
   ``operations-control-request`` contract and the desired phase booleans must
   satisfy ``prepare >= dispatch >= execute`` (fail closed otherwise).
#. **Build.** The next kill-switch document is built with a monotonically
   advanced ``config_version`` (current + 1) and a fresh
   ``issued_at``/``not_after`` horizon, and validated against the immutable
   ``operations-kill-switch`` contract.
#. **Commit (atomic CAS + audit).** The change is committed through the control
   audit store with a compare-and-set on ``expected_config_version``. The store
   writes an immutable intent record, then advances the state and writes the
   immutable outcome audit record together in ONE atomic ``TransactWriteItems``
   call (authorized by the underlying ``dynamodb:UpdateItem`` and
   ``dynamodb:PutItem`` actions the store already holds — there is no separate
   ``dynamodb:TransactWriteItems`` permission). A stale/racing write fails the
   CAS and returns ``version_conflict`` — no document is published. A transient
   store failure fails closed (raises), and because the commit is atomic a retry
   recovers the whole decision without ever advancing the version without audit.
#. **Publish.** Only after a committed CAS is the document published to AppConfig,
   selecting the *immediate* strategy for a hard-down (any reduction relative to
   the current document) and the *gradual* strategy for a normal change. A
   publish failure after a committed CAS raises (the caller retries) rather than
   reporting success.

Identity never enters the request or the response body: the acting admin is
resolved from the verified principal and recorded only in the audit store.
"""

from __future__ import annotations

# Standard library
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.contracts.control_plane import (
    CAPABILITY_ID,
    CONTROL_CONTRACT_VERSION,
    CONTROL_PHASES,
    CONTROL_REQUEST_SCHEMA_NAME,
    CONTROL_RESPONSE_SCHEMA_NAME,
    KILL_SWITCH_SCHEMA_NAME,
    ControlContractError,
    desired_switches_ordered,
    validate_control_contract,
)
from operations.control.control_audit_store import ControlCommitOutcome, ControlStoreError

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


class ControlServiceError(RuntimeError):
    """A bounded, public-safe admin control failure (fail closed)."""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        super().__init__(safe_message)


class AuditStorePort(Protocol):
    """CAS control-state + immutable audit store port."""

    def current_config_version(self) -> int | None: ...

    def commit_control_decision(
        self,
        *,
        record_id: str,
        actor: dict[str, str],
        expected_config_version: int,
        desired: dict[str, Any],
        outcome: str,
        resulting_config_version: int,
    ) -> ControlCommitOutcome: ...

    # Optional publisher lost-response reconciliation. A store that predates
    # these is tolerated: the service treats missing methods as "no pending
    # publication to reconcile" and behaves exactly as before.
    def pending_publication(self, *, record_id: str) -> dict[str, Any] | None: ...

    def confirm_publication(self, *, record_id: str) -> None: ...


class PublisherPort(Protocol):
    """AppConfig kill-switch publisher port."""

    def publish(self, *, document: dict[str, Any], hard_down: bool) -> None: ...


class KillSwitchControlService:
    """Admin-only, CAS, audited kill-switch control."""

    def __init__(
        self,
        *,
        audit_store: AuditStorePort,
        publisher: PublisherPort,
        admin_group: str,
        clock: Callable[[], datetime] | None = None,
        freshness_seconds: int = 600,
        metrics: Any = None,
    ) -> None:
        if not isinstance(admin_group, str) or not admin_group.strip():
            raise ValueError("admin_group must be a non-empty string")
        if freshness_seconds <= 0:
            raise ValueError("freshness_seconds must be positive")
        self._audit_store = audit_store
        self._publisher = publisher
        self._admin_group = admin_group
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._freshness = timedelta(seconds=freshness_seconds)
        # Optional, dimensionless control-plane metrics sink (issue #416). Every
        # emit is best-effort and must never break the control flow.
        self._metrics = metrics

    def _emit(self, event_name: str) -> None:
        sink = self._metrics
        if sink is None:
            return
        try:
            sink.record(event_name)
        except Exception:  # noqa: BLE001 - metrics must never break control
            pass

    def apply(
        self,
        *,
        request: dict[str, Any],
        principal: Any,
        current_document: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one admin control request as an atomic, audited CAS change."""
        self._require_admin(principal)
        desired = self._validated_desired(request)
        expected_config_version = int(request["expected_config_version"])

        current_version = self._audit_store.current_config_version()
        resulting_version = expected_config_version + 1

        hard_down = _is_hard_down(desired, current_document)
        document = self._build_document(desired, resulting_version)

        actor = {
            "subject_id": getattr(principal, "subject_id", ""),
            "client_id": getattr(principal, "client_id", ""),
        }
        record_id = self._record_id(expected_config_version, desired, actor)

        try:
            outcome = self._audit_store.commit_control_decision(
                record_id=record_id,
                actor=actor,
                expected_config_version=expected_config_version,
                desired=desired,
                outcome="applied",
                resulting_config_version=resulting_version,
            )
        except ControlStoreError as exc:
            raise ControlServiceError("CONTROL_UNAVAILABLE", "control change could not be recorded") from exc

        if outcome is ControlCommitOutcome.VERSION_CONFLICT:
            # A conflict may be a genuine concurrent-admin race, OR it may be a
            # retry of THIS exact decision whose earlier publish response was
            # lost after the CAS had already committed. If the store still holds
            # an unconfirmed publication marker for this record_id, reconcile by
            # re-driving the idempotent publish and confirming it — reporting the
            # now-live document as applied rather than a bare version_conflict
            # that would hide a version-advanced, unpublished decision. The CAS
            # is never re-run here, so this cannot weaken the compare-and-set.
            reconciled = self._reconcile_pending_publication(record_id=record_id, desired=desired, hard_down=hard_down)
            if reconciled is not None:
                return reconciled
            self._emit("control.version_conflict")
            return self._response(
                outcome="version_conflict",
                config_version=current_version if isinstance(current_version, int) else expected_config_version,
                reason_code="VERSION_CONFLICT",
            )

        # Committed: the state advance and its outcome audit record are durably
        # persisted (one atomic transaction), together with an unconfirmed
        # publication marker. Publish the now-authoritative document. A publish
        # failure after the CAS committed is surfaced (the caller retries); we
        # never report success for a document that did not go live.
        try:
            self._publisher.publish(document=document, hard_down=hard_down)
        except Exception as exc:  # noqa: BLE001 - bounded, fail closed
            raise ControlServiceError(
                "CONTROL_PUBLISH_FAILED", "control change was recorded but not published"
            ) from exc

        # AppConfig acknowledged the publish: confirm the durable marker so a
        # later retry does not needlessly re-publish. Confirmation is best-effort
        # and idempotent; a failure here still leaves a reconcilable pending
        # marker rather than a false "applied", so it must not mask the success.
        self._confirm_publication(record_id)

        self._emit("control.applied")
        return self._response(
            outcome="applied",
            config_version=resulting_version,
            reason_code="APPLIED",
            effective=document,
        )

    def _reconcile_pending_publication(
        self, *, record_id: str, desired: dict[str, Any], hard_down: bool
    ) -> dict[str, Any] | None:
        """Re-publish and confirm an own committed-but-unpublished decision.

        Returns an ``applied`` response when the store holds an unconfirmed
        publication marker for ``record_id`` (a lost-response retry), or ``None``
        when there is nothing of ours to reconcile (a genuine conflict). The
        stored ``config_version`` from the marker is authoritative — the document
        is rebuilt at that version and re-published idempotently.
        """
        pending = self._pending_publication(record_id)
        if pending is None:
            return None
        config_version = pending.get("config_version")
        if not isinstance(config_version, int):
            return None
        document = self._build_document(desired, config_version)
        try:
            self._publisher.publish(document=document, hard_down=hard_down)
        except Exception as exc:  # noqa: BLE001 - bounded, fail closed
            raise ControlServiceError(
                "CONTROL_PUBLISH_FAILED", "control change was recorded but not published"
            ) from exc
        self._confirm_publication(record_id)
        self._emit("control.publication_reconciled")
        return self._response(
            outcome="applied",
            config_version=config_version,
            reason_code="APPLIED",
            effective=document,
        )

    def _pending_publication(self, record_id: str) -> dict[str, Any] | None:
        getter = getattr(self._audit_store, "pending_publication", None)
        if getter is None:
            return None
        try:
            result = getter(record_id=record_id)
        except Exception:  # noqa: BLE001 - reconciliation is best-effort, never fatal
            return None
        return result if isinstance(result, dict) else None

    def _confirm_publication(self, record_id: str) -> None:
        confirmer = getattr(self._audit_store, "confirm_publication", None)
        if confirmer is None:
            return
        try:
            confirmer(record_id=record_id)
        except Exception:  # noqa: BLE001 - confirmation is best-effort, idempotent
            pass

    # -- internals -------------------------------------------------------

    def _require_admin(self, principal: Any) -> None:
        groups: frozenset[str] = getattr(principal, "groups", frozenset())
        if self._admin_group not in groups:
            self._emit("control.denied")
            raise ControlServiceError("AUTHORIZATION_DENIED", "control requires the admin group")

    def _validated_desired(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_control_contract(CONTROL_REQUEST_SCHEMA_NAME, request)
        except ControlContractError as exc:
            raise ControlServiceError("CONTRACT_INVALID", "control request is invalid") from exc
        desired: dict[str, Any] = request["desired"]
        switches = desired["capabilities"][CAPABILITY_ID]
        if not desired_switches_ordered(switches):
            raise ControlServiceError("PHASE_ORDER_INVALID", "desired phases violate prepare >= dispatch >= execute")
        if not desired["operations_enabled"] and any(switches[phase] for phase in CONTROL_PHASES):
            raise ControlServiceError("PHASE_ORDER_INVALID", "operations disabled but a phase is enabled")
        return desired

    def _build_document(self, desired: dict[str, Any], config_version: int) -> dict[str, Any]:
        now = self._clock().astimezone(timezone.utc)
        switches = desired["capabilities"][CAPABILITY_ID]
        document = {
            "contract_version": CONTROL_CONTRACT_VERSION,
            "config_version": config_version,
            "issued_at": _iso(now),
            "not_after": _iso(now + self._freshness),
            "operations_enabled": bool(desired["operations_enabled"]),
            "capabilities": {
                CAPABILITY_ID: {phase: bool(switches[phase]) for phase in CONTROL_PHASES},
            },
        }
        try:
            validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)
        except ControlContractError as exc:  # pragma: no cover - server-owned output
            raise ControlServiceError("CONTRACT_OUTPUT_INVALID", "built kill-switch document is invalid") from exc
        return document

    def _record_id(self, expected_config_version: int, desired: dict[str, Any], actor: dict[str, str]) -> str:
        fingerprint = json.dumps(
            {
                "expected_config_version": expected_config_version,
                "desired": desired,
                "actor": actor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"ctl_{_hex_to_id_body(digest)}"

    def _response(
        self,
        *,
        outcome: str,
        config_version: int,
        reason_code: str,
        effective: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "outcome": outcome,
            "config_version": int(config_version),
            "reason_code": reason_code,
        }
        if effective is not None:
            response["effective"] = effective
        validate_control_contract(CONTROL_RESPONSE_SCHEMA_NAME, response)
        return response


def _is_hard_down(desired: dict[str, Any], current_document: dict[str, Any] | None) -> bool:
    """Return whether ``desired`` reduces authority relative to the current doc.

    A hard-down is any flag turning OFF: the master switch, or any phase boolean.
    Without a current document we cannot prove a reduction, so we treat it as a
    normal (gradual) change; the immediate strategy is reserved for a proven
    reduction so a hard-down always propagates as fast as AppConfig allows.
    """
    if current_document is None:
        # No baseline: a fully-disabled desired state is itself a hard-down.
        return not desired["operations_enabled"]
    if current_document.get("operations_enabled") and not desired["operations_enabled"]:
        return True
    current_switches = (current_document.get("capabilities") or {}).get(CAPABILITY_ID) or {}
    desired_switches = desired["capabilities"][CAPABILITY_ID]
    for phase in CONTROL_PHASES:
        if current_switches.get(phase) and not desired_switches[phase]:
            return True
    return False


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hex_to_id_body(digest: str) -> str:
    value = int(digest, 16)
    body = []
    for _ in range(26):
        value, index = divmod(value, 36)
        body.append(_ID_ALPHABET[index])
    return "".join(reversed(body))
