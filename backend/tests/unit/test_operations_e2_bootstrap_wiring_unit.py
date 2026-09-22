"""Bootstrap-wiring tests for the deployable E2 control plane (issue #414).

These build the *real* deployable handler over injected fake AWS clients and
assert the security-critical wiring the on-host handler will actually use:

* the approval policy requires the server-owned ``admin`` group and carries no
  scope (so a client-id/scope match can never authorize an approver);
* the prepared operation's playbook hash is the real definition digest, not the
  all-zero placeholder;
* the capacity bounds resolver is injected with the server-owned fail-closed
  settings, not hardcoded million-instance limits; and
* the approval service is injected with the capacity-specific validator,
  hasher, and binding (not the generic source-control defaults), so it can
  validate and hash the stored capacity operation it is asked to approve.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.playbook_definition import capacity_playbook_hash

_COMPLETE_ENV = {
    "GBAW_OPERATIONS_MODE": "advise",
    "GBAW_OPERATIONS_TABLE_NAME": "operations-observations",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "operations-api",
}


class _FakeSession:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def client(self, name: str, **kwargs: Any) -> Any:
        return object()


def _build_router(monkeypatch: pytest.MonkeyPatch, **env_overrides: str):
    # Local modules
    import operations.observe.lambda_entry as entry

    env = dict(_COMPLETE_ENV, **env_overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.Session", _FakeSession, raising=False)
    monkeypatch.setattr(entry, "_region", lambda: "us-west-2")
    entry._handler.cache_clear()
    router = entry._handler()
    entry._handler.cache_clear()
    return router


def test_bootstrap_policy_requires_admin_group_and_no_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _build_router(monkeypatch)
    policy = router._approval_handler._approval_service._policy
    assert policy.approver_groups == frozenset({"admin"})
    assert policy.approver_scopes == frozenset()


def test_bootstrap_policy_honors_overridden_approver_group(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _build_router(monkeypatch, GBAW_OPERATIONS_APPROVER_GROUP="operations-approvers")
    policy = router._approval_handler._approval_service._policy
    assert policy.approver_groups == frozenset({"operations-approvers"})
    assert policy.approver_scopes == frozenset()


def test_bootstrap_playbook_hash_is_the_real_definition_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _build_router(monkeypatch)
    orchestrator = router._approval_handler._orchestrator
    playbook = orchestrator._prepare_service._playbook
    assert playbook.playbook_hash == capacity_playbook_hash()
    assert playbook.playbook_hash != "sha256:" + "0" * 64


def test_bootstrap_default_bounds_are_the_fail_closed_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _build_router(monkeypatch)
    factory = router._approval_handler._orchestrator._advice_service_factory
    advice_service = factory("obs_aaaaaaaaaaaaaaaaaaaaaaaaaa")
    bounds_port = advice_service._bounds_port
    assert (bounds_port._floor, bounds_port._ceiling, bounds_port._max_step) == (0, 1, 1)


def test_bootstrap_bounds_reflect_configured_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _build_router(
        monkeypatch,
        GBAW_OPERATIONS_CAPACITY_FLOOR="2",
        GBAW_OPERATIONS_CAPACITY_CEILING="10",
        GBAW_OPERATIONS_CAPACITY_MAX_STEP="3",
    )
    factory = router._approval_handler._orchestrator._advice_service_factory
    advice_service = factory("obs_aaaaaaaaaaaaaaaaaaaaaaaaaa")
    bounds_port = advice_service._bounds_port
    assert (bounds_port._floor, bounds_port._ceiling, bounds_port._max_step) == (2, 10, 3)


def test_bootstrap_injects_capacity_validation_hash_and_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for the live E2 defect (#414): the bootstrap must inject the
    # three capacity-specific functions into ApprovalService. Without them the
    # service falls back to the generic source-control contract and rejects
    # every stored capacity operation as APPROVAL_INVALID. The generic module
    # defaults must remain the fallback for other callers (#419), so assert the
    # injected callables are the capacity functions and are NOT the defaults.
    # Local modules
    from operations.approval import (
        _default_binding_validate,
        _default_hash,
        _default_validate,
    )
    from operations.contracts.capacity import (
        capacity_prepared_hash,
        validate_capacity_approval_binding,
        validate_capacity_prepared_operation,
    )

    router = _build_router(monkeypatch)
    approval_service = router._approval_handler._approval_service

    assert approval_service._operation_validator is validate_capacity_prepared_operation
    assert approval_service._operation_hasher is capacity_prepared_hash
    assert approval_service._binding_validator is validate_capacity_approval_binding

    assert approval_service._operation_validator is not _default_validate
    assert approval_service._operation_hasher is not _default_hash
    assert approval_service._binding_validator is not _default_binding_validate


def test_generic_approval_service_defaults_remain_backward_compatible() -> None:
    # #419 backward-compatibility guard: constructing ApprovalService without the
    # capacity hooks must still bind the generic source-control validator,
    # hasher, and binding, so the generic approval path is unaffected by the E2
    # capacity injection.
    # Local modules
    from operations.approval import (
        ApprovalPolicy,
        ApprovalService,
        _default_binding_validate,
        _default_hash,
        _default_validate,
    )
    from operations.identity import ApprovalIdentityBoundary

    boundary = ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"operations-api"}),
        approver_client_ids=frozenset({"operations-api"}),
        trusted_audiences=frozenset({"operations-api"}),
    )
    policy = ApprovalPolicy(
        policy_id="policy.source-control",
        policy_version="1",
        approver_groups=frozenset({"admin"}),
    )
    service = ApprovalService(identity_boundary=boundary, policy=policy, store=object())

    assert service._operation_validator is _default_validate
    assert service._operation_hasher is _default_hash
    assert service._binding_validator is _default_binding_validate
