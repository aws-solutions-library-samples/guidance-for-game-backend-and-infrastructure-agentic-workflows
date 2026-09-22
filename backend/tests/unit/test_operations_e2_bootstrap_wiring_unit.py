"""Bootstrap-wiring tests for the deployable E2 control plane (issue #414).

These build the *real* deployable handler over injected fake AWS clients and
assert the security-critical wiring the on-host handler will actually use:

* the approval policy requires the server-owned ``admin`` group and carries no
  scope (so a client-id/scope match can never authorize an approver);
* the prepared operation's playbook hash is the real definition digest, not the
  all-zero placeholder;
* the capacity bounds resolver is injected with the server-owned fail-closed
  settings, not hardcoded million-instance limits.
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
