"""Admin kill-switch control service tests (issue #416, E4).

:class:`~operations.control.control_service.KillSwitchControlService` is the
protocol-neutral core of the admin control plane. Given a verified admin
principal and a control request that carries ONLY desired booleans plus the
expected config_version (never identity), it:

* requires the acting principal to be an admin (fail closed otherwise);
* enforces the phase-ordering invariant on the desired state
  (prepare >= dispatch >= execute), failing closed on an invalid shape;
* builds the next kill-switch document with a monotonically advanced
  config_version and freshness horizon;
* commits the change with a compare-and-set on expected_config_version through
  the control audit store (immutable intent + outcome records), returning a
  version_conflict on a stale/racing write without publishing; and
* publishes the new document through the AppConfig publisher, selecting the
  immediate strategy for a hard-down (any reduction) and the gradual strategy for
  a normal change.

It returns a validated ``operations-control-response`` and never places identity
in the request or response body.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID, CONTROL_RESPONSE_SCHEMA_NAME, validate_control_contract
from operations.control.control_audit_store import ControlCommitOutcome, ControlStoreError
from operations.control.control_service import ControlServiceError, KillSwitchControlService

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Principal:
    def __init__(self, groups: frozenset[str], subject_id: str = "admin-1", client_id: str = "client-1") -> None:
        self.groups = groups
        self.subject_id = subject_id
        self.client_id = client_id


class _FakeAuditStore:
    def __init__(self, current: int | None, outcome: ControlCommitOutcome = ControlCommitOutcome.COMMITTED) -> None:
        self._current = current
        self._outcome = outcome
        self.commits: list[dict[str, Any]] = []
        self.reconciliations: list[dict[str, Any]] = []
        self.initialized: list[int] = []
        self.raise_on_commit: Exception | None = None

    def initialize_state_if_absent(self, *, config_version: int) -> None:
        self.initialized.append(config_version)
        if self._current is None:
            self._current = config_version

    def current_config_version(self) -> int | None:
        return self._current

    def commit_control_decision(self, **kwargs: Any) -> ControlCommitOutcome:
        self.commits.append(kwargs)
        if self.raise_on_commit is not None:
            raise self.raise_on_commit
        return self._outcome

    def reconcile_external_control(self, **kwargs: Any) -> ControlCommitOutcome:
        self.reconciliations.append(kwargs)
        if self._current != kwargs["expected_config_version"]:
            return ControlCommitOutcome.VERSION_CONFLICT
        self._current = kwargs["resulting_config_version"]
        return ControlCommitOutcome.COMMITTED

    # -- publication reconciliation (optional port) ----------------------
    # ``pending`` maps record_id -> {"config_version": int, "published": False}
    # for a committed-but-unconfirmed decision. ``confirmed`` records the ids the
    # service confirmed after a successful publish.
    pending: dict[str, dict[str, Any]]
    confirmed: list[str]

    def pending_publication(self, *, record_id: str) -> dict[str, Any] | None:
        return getattr(self, "pending", {}).get(record_id)

    def confirm_publication(self, *, record_id: str) -> None:
        if not hasattr(self, "confirmed"):
            self.confirmed = []
        self.confirmed.append(record_id)
        pending = getattr(self, "pending", {})
        pending.pop(record_id, None)


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.reconciled: list[dict[str, Any]] = []
        self.legacy_document: dict[str, Any] | None = None
        self.raise_on_publish: Exception | None = None

    def publish(self, *, document: dict[str, Any], hard_down: bool) -> None:
        self.published.append({"document": document, "hard_down": hard_down})
        if self.raise_on_publish is not None:
            raise self.raise_on_publish

    def reconcile_existing(self, **kwargs: Any) -> dict[str, Any]:
        self.reconciled.append(kwargs)
        if self.legacy_document is None:
            raise RuntimeError("no legacy document")
        return self.legacy_document


def _service(audit: Any, publisher: Any) -> KillSwitchControlService:
    return KillSwitchControlService(
        audit_store=audit,
        publisher=publisher,
        admin_group="admin",
        clock=lambda: _NOW,
        freshness_seconds=600,
    )


def _desired(enabled: bool, prepare: bool, dispatch: bool, execute: bool) -> dict[str, Any]:
    return {
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": prepare, "dispatch": dispatch, "execute": execute}},
    }


def _current_document(version: int, *, enabled: bool, prepare: bool, dispatch: bool, execute: bool) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "config_version": version,
        "issued_at": "2026-01-01T11:59:00Z",
        "not_after": "2026-01-01T12:10:00Z",
        **_desired(enabled, prepare, dispatch, execute),
    }


def _request(desired: dict[str, Any], expected_config_version: int) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "expected_config_version": expected_config_version,
        "desired": desired,
    }


def _admin() -> _Principal:
    return _Principal(groups=frozenset({"admin"}))


# -- authorization ----------------------------------------------------------


def test_non_admin_is_denied() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(_desired(True, True, True, True), 1), principal=_Principal(frozenset({"users"})))
    assert not audit.commits
    assert not publisher.published


def test_invalid_phase_order_is_rejected() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    # execute on without dispatch violates prepare>=dispatch>=execute.
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(_desired(True, True, False, True), 1), principal=_admin())
    assert not audit.commits
    assert not publisher.published


# -- apply ------------------------------------------------------------------


def test_normal_enable_commits_and_publishes_gradual() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    validate_control_contract(CONTROL_RESPONSE_SCHEMA_NAME, response)
    assert response["outcome"] == "applied"
    assert response["config_version"] == 2
    assert audit.commits[0]["expected_config_version"] == 1
    assert audit.commits[0]["resulting_config_version"] == 2
    assert audit.commits[0]["publication_document"] == publisher.published[0]["document"]
    assert publisher.published[0]["hard_down"] is False


def test_initial_stale_safe_seed_recovers_through_admin_control() -> None:
    audit = _FakeAuditStore(current=None)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, False, False), 1), principal=_admin())
    assert audit.initialized == [1]
    assert response["outcome"] == "applied"
    assert response["config_version"] == 2
    assert audit.commits[0]["expected_config_version"] == 1
    assert publisher.published[0]["hard_down"] is False


def test_uninitialized_state_rejects_non_seed_expected_version() -> None:
    audit = _FakeAuditStore(current=None)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, False, False), 9), principal=_admin())
    assert response["outcome"] == "version_conflict"
    assert not audit.initialized
    assert not audit.commits
    assert not publisher.published


def test_fresh_external_hard_down_is_audited_before_reenable() -> None:
    audit = _FakeAuditStore(current=2)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    emergency = _current_document(
        1_800_000_000,
        enabled=False,
        prepare=False,
        dispatch=False,
        execute=False,
    )
    response = service.apply(
        request=_request(_desired(True, True, False, False), 1_800_000_000),
        principal=_admin(),
        current_document=emergency,
    )
    assert audit.reconciliations
    reconciliation = audit.reconciliations[0]
    assert reconciliation["expected_config_version"] == 2
    assert reconciliation["resulting_config_version"] == 1_800_000_000
    assert reconciliation["desired"] == _desired(False, False, False, False)
    assert audit.commits[0]["expected_config_version"] == 1_800_000_000
    assert response["config_version"] == 1_800_000_001
    assert publisher.published[0]["hard_down"] is False


def test_hard_down_selects_immediate_publish() -> None:
    # Current is fully enabled (v1); desired disables everything -> hard-down.
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(
        request=_request(_desired(False, False, False, False), 1),
        principal=_admin(),
        current_document=_current_document(1, enabled=True, prepare=True, dispatch=True, execute=True),
    )
    assert response["outcome"] == "applied"
    assert publisher.published[0]["hard_down"] is True


def test_phase_only_reduction_selects_immediate_publish() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(
        request=_request(_desired(True, True, False, False), 1),
        principal=_admin(),
        current_document=_current_document(1, enabled=True, prepare=True, dispatch=True, execute=True),
    )
    assert response["outcome"] == "applied"
    assert publisher.published[0]["hard_down"] is True


def test_version_conflict_does_not_publish() -> None:
    audit = _FakeAuditStore(current=5, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert response["outcome"] == "version_conflict"
    assert response["config_version"] == 5  # current stored version to reconcile against
    assert not publisher.published


def test_commit_transient_failure_fails_closed() -> None:
    audit = _FakeAuditStore(current=1)
    audit.raise_on_commit = ControlStoreError("throttled")
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert not publisher.published


def test_publish_failure_after_commit_raises() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    publisher.raise_on_publish = RuntimeError("appconfig down")
    service = _service(audit, publisher)
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    # The CAS committed but publish failed: the caller must retry, not see success.
    assert audit.commits


def test_no_identity_in_request_or_response() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    blob = repr(response)
    for identity in ("admin-1", "client-1", "subject_id", "actor"):
        assert identity not in blob
    # The actor is recorded in the audit store commit, never in the response.
    assert audit.commits[0]["actor"] == {"subject_id": "admin-1", "client_id": "client-1"}


def test_expected_version_must_match_desired_request() -> None:
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    # A malformed request (missing desired) fails closed.
    with pytest.raises(ControlServiceError):
        service.apply(request={"contract_version": "1.0", "expected_config_version": 1}, principal=_admin())


# -- publisher lost-response reconciliation ---------------------------------


def test_successful_apply_confirms_the_publication() -> None:
    """After AppConfig acknowledges the publish, the service confirms it."""
    audit = _FakeAuditStore(current=1)
    audit.pending = {}
    audit.confirmed = []
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert response["outcome"] == "applied"
    # Exactly one publication was confirmed, and it matches the committed record.
    assert len(audit.confirmed) == 1
    assert audit.confirmed[0] == audit.commits[0]["record_id"]


def test_conflict_with_own_pending_publication_reconciles_to_applied() -> None:
    """A retry after a lost publish response re-publishes and reports applied.

    The prior attempt committed the CAS (version already advanced) but its
    publish response was lost, so this retry's commit returns VERSION_CONFLICT.
    Because the store still holds an UNCONFIRMED publication marker for THIS
    record_id, the service re-drives the idempotent publish, confirms it, and
    reports the now-live document as applied — never a bare version_conflict that
    would hide an un-published, version-advanced decision.
    """
    audit = _FakeAuditStore(current=2, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    # Compute the deterministic record_id the service will derive for this request
    # by running one commit against a COMMITTED store, then reuse it as pending.
    probe = _FakeAuditStore(current=1)
    probe.pending = {}
    probe.confirmed = []
    _service(probe, _FakePublisher()).apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    record_id = probe.commits[0]["record_id"]
    pending_document = _current_document(
        2,
        enabled=True,
        prepare=True,
        dispatch=True,
        execute=True,
    )
    audit.pending = {
        record_id: {
            "config_version": 2,
            "published": False,
            "document": pending_document,
        }
    }
    audit.confirmed = []

    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert response["outcome"] == "applied"
    assert response["config_version"] == 2
    # The exact durably stored bytes are republished; retry time never changes
    # issued_at/not_after under the same deterministic provider label.
    assert publisher.published[0]["document"] == pending_document
    assert audit.confirmed == [record_id]


def test_legacy_pending_marker_recovers_from_the_validated_hosted_version() -> None:
    audit = _FakeAuditStore(current=2, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    publisher = _FakePublisher()
    publisher.legacy_document = _current_document(
        2,
        enabled=True,
        prepare=True,
        dispatch=True,
        execute=True,
    )
    service = _service(audit, publisher)
    probe = _FakeAuditStore(current=1)
    probe.pending = {}
    probe.confirmed = []
    _service(probe, _FakePublisher()).apply(
        request=_request(_desired(True, True, True, True), 1),
        principal=_admin(),
    )
    record_id = probe.commits[0]["record_id"]
    audit.pending = {record_id: {"config_version": 2, "published": False}}
    audit.confirmed = []

    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert response["outcome"] == "applied"
    assert response["effective"] == publisher.legacy_document
    assert publisher.reconciled == [
        {
            "config_version": 2,
            "desired": _desired(True, True, True, True),
            "hard_down": False,
        }
    ]
    assert publisher.published == []
    assert audit.confirmed == [record_id]


def test_stored_pending_document_with_different_authority_fails_closed() -> None:
    audit = _FakeAuditStore(current=2, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    probe = _FakeAuditStore(current=1)
    probe.pending = {}
    probe.confirmed = []
    _service(probe, _FakePublisher()).apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    record_id = probe.commits[0]["record_id"]
    audit.pending = {
        record_id: {
            "config_version": 2,
            "published": False,
            "document": _current_document(2, enabled=False, prepare=False, dispatch=False, execute=False),
        }
    }
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert publisher.published == []


def test_legacy_reconciler_returning_different_authority_fails_closed() -> None:
    audit = _FakeAuditStore(current=2, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    publisher = _FakePublisher()
    publisher.legacy_document = _current_document(2, enabled=False, prepare=False, dispatch=False, execute=False)
    service = _service(audit, publisher)
    probe = _FakeAuditStore(current=1)
    probe.pending = {}
    probe.confirmed = []
    _service(probe, _FakePublisher()).apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    record_id = probe.commits[0]["record_id"]
    audit.pending = {record_id: {"config_version": 2, "published": False}}
    audit.confirmed = []
    with pytest.raises(ControlServiceError):
        service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert audit.confirmed == []


def test_legacy_marker_without_reconciler_remains_a_version_conflict() -> None:
    class _OldPublisher:
        def __init__(self) -> None:
            self.published = 0

        def publish(self, **kwargs: Any) -> None:
            self.published += 1

    audit = _FakeAuditStore(current=2, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    old_publisher = _OldPublisher()
    service = _service(audit, old_publisher)
    probe = _FakeAuditStore(current=1)
    probe.pending = {}
    probe.confirmed = []
    _service(probe, _FakePublisher()).apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    record_id = probe.commits[0]["record_id"]
    audit.pending = {record_id: {"config_version": 2, "published": False}}
    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert response["outcome"] == "version_conflict"
    assert old_publisher.published == 0


def test_conflict_without_own_pending_publication_stays_version_conflict() -> None:
    """A genuine concurrent-admin conflict (no pending marker) is unchanged."""
    audit = _FakeAuditStore(current=5, outcome=ControlCommitOutcome.VERSION_CONFLICT)
    audit.pending = {}
    audit.confirmed = []
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(request=_request(_desired(True, True, True, True), 1), principal=_admin())
    assert response["outcome"] == "version_conflict"
    assert response["config_version"] == 5
    assert not publisher.published
    assert not audit.confirmed
