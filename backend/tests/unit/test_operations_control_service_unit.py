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
from operations.control.control_audit_store import ControlCommitOutcome, ControlStoreError
from operations.control.control_service import ControlServiceError, KillSwitchControlService
from operations.contracts.control_plane import (
    CAPABILITY_ID,
    CONTROL_RESPONSE_SCHEMA_NAME,
    validate_control_contract,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Principal:
    def __init__(self, groups: frozenset[str], subject_id: str = "admin-1", client_id: str = "client-1") -> None:
        self.groups = groups
        self.subject_id = subject_id
        self.client_id = client_id


class _FakeAuditStore:
    def __init__(self, current: int, outcome: ControlCommitOutcome = ControlCommitOutcome.COMMITTED) -> None:
        self._current = current
        self._outcome = outcome
        self.commits: list[dict[str, Any]] = []
        self.raise_on_commit: Exception | None = None

    def current_config_version(self) -> int:
        return self._current

    def commit_control_decision(self, **kwargs: Any) -> ControlCommitOutcome:
        self.commits.append(kwargs)
        if self.raise_on_commit is not None:
            raise self.raise_on_commit
        return self._outcome


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.raise_on_publish: Exception | None = None

    def publish(self, *, document: dict[str, Any], hard_down: bool) -> None:
        self.published.append({"document": document, "hard_down": hard_down})
        if self.raise_on_publish is not None:
            raise self.raise_on_publish


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
    assert publisher.published[0]["hard_down"] is False


def test_hard_down_selects_immediate_publish() -> None:
    # Current is fully enabled (v1); desired disables everything -> hard-down.
    audit = _FakeAuditStore(current=1)
    publisher = _FakePublisher()
    service = _service(audit, publisher)
    response = service.apply(
        request=_request(_desired(False, False, False, False), 1),
        principal=_admin(),
        current_document=_desired(True, True, True, True),
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
