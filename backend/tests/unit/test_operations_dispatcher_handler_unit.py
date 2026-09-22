"""E3 dispatcher handler tests (#415, E3 execute).

The dispatcher is the authenticated HTTP boundary that starts one execution
workflow. It trusts identity ONLY from the API Gateway JWT authorizer context,
requires the admin group, loads the approved workspace-scoped operation, and
starts a Step Functions Standard execution with ONLY ``{operation_id}`` and a
stable, deterministic execution name — never any executable content, playbook,
credential, capacity value, or handler-derived payload.

These tests assert:

* unauthenticated / missing-claims requests are denied (401), no SFN start;
* a non-admin caller is denied (403), no SFN start;
* a caller from a different workspace is denied (403/404), no SFN start;
* an operation not in the approved state is a conflict (409), no SFN start;
* a valid admin dispatch starts exactly one SFN execution whose input is exactly
  {"operation_id": ...} (no other key) and whose name is stable across retries;
* a duplicate dispatch reuses the same stable execution name (idempotent).
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.dispatcher_handler import DispatcherRequestHandler

_EXP = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_STATE_MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:gbaw-executor"


class _FakeSfn:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.starts.append(kwargs)
        return {"executionArn": "arn:aws:states:us-west-2:123456789012:execution:gbaw-executor:x"}


class _FakeStore:
    def __init__(
        self, *, state: str = "approved", tenant: str = "tenant.default", workspace: str = "workspace.default"
    ) -> None:
        self._state = state
        self._tenant = tenant
        self._workspace = workspace

    def load_dispatch_view(self, operation_id: str) -> dict[str, Any] | None:
        if operation_id != _OP:
            return None
        return {
            "operation_id": operation_id,
            "state": self._state,
            "tenant_id": self._tenant,
            "workspace_id": self._workspace,
        }


def _handler(sfn: _FakeSfn, store: _FakeStore) -> DispatcherRequestHandler:
    return DispatcherRequestHandler(
        store=store,
        step_functions=sfn,
        state_machine_arn=_STATE_MACHINE_ARN,
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience="aud-client",
        admin_group="admin",
    )


def _event(
    *, groups: object = "[admin]", claims_present: bool = True, client_id: str = "client.web-console"
) -> dict[str, Any]:
    if not claims_present:
        return {"requestContext": {"http": {"method": "POST"}}, "pathParameters": {"operationId": _OP}}
    return {
        "requestContext": {
            "http": {"method": "POST"},
            "requestId": "req-1",
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "subject.admin-1",
                        "client_id": client_id,
                        "token_use": "access",
                        "aud": "aud-client",
                        "exp": _EXP,
                        "cognito:groups": groups,
                        "scope": "operations/execute",
                        "custom:tenant_id": "tenant.default",
                        "custom:workspace_id": "workspace.default",
                    }
                }
            },
        },
        "pathParameters": {"operationId": _OP},
    }


def test_missing_claims_denied_no_start() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(claims_present=False))
    assert resp["statusCode"] == 401
    assert sfn.starts == []


def test_non_admin_denied_no_start() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(groups="[users]"))
    assert resp["statusCode"] == 403
    assert sfn.starts == []


def test_wrong_workspace_denied_no_start() -> None:
    sfn = _FakeSfn()
    store = _FakeStore(workspace="workspace.other")
    resp = _handler(sfn, store).handle(_event())
    assert resp["statusCode"] in {403, 404}
    assert sfn.starts == []


def test_non_approved_state_conflict_no_start() -> None:
    sfn = _FakeSfn()
    store = _FakeStore(state="pending_approval")
    resp = _handler(sfn, store).handle(_event())
    assert resp["statusCode"] == 409
    assert sfn.starts == []


def test_valid_admin_dispatch_starts_only_operation_id() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event())
    assert resp["statusCode"] in {200, 202}
    assert len(sfn.starts) == 1
    start = sfn.starts[0]
    assert start["stateMachineArn"] == _STATE_MACHINE_ARN
    # The input is EXACTLY {"operation_id": ...} — no executable content.
    payload = json.loads(start["input"])
    assert payload == {"operation_id": _OP}
    # A stable, non-empty execution name.
    assert isinstance(start["name"], str) and start["name"]


def test_dispatch_execution_name_is_stable() -> None:
    sfn1, sfn2 = _FakeSfn(), _FakeSfn()
    _handler(sfn1, _FakeStore()).handle(_event())
    _handler(sfn2, _FakeStore()).handle(_event())
    assert sfn1.starts[0]["name"] == sfn2.starts[0]["name"]
