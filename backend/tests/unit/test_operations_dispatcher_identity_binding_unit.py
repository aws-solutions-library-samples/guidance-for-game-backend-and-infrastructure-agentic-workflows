"""E3 dispatcher identity-binding regression tests (#415, E3 execute).

Live E3 dispatch reached the ``/dispatch`` route, but both a real ``admin`` and a
real ``users`` Cognito **access** token were rejected with 401
``IDENTITY_CONTEXT_INVALID``. The dispatcher's ``_verified_principal`` read the
audience from the token's ``aud`` claim and the tenant/workspace from
``custom:tenant_id``/``custom:workspace_id`` custom claims. A Cognito **access**
token carries neither: it identifies the app client with ``client_id`` (there is
no ``aud``), and it carries no ``custom:*`` tenant/workspace claims. Those lookups
returned ``None`` -> empty string -> :class:`VerifiedPrincipal` identifier
validation failed -> 401, so no real token could ever dispatch.

The architecture — exactly as the E1 (observation) and E2 (approval) handlers do
it — binds ``tenant_id``, ``workspace_id`` and ``trusted_audience`` from
**server-owned deployment config**, and derives only ``subject``, ``client_id``,
``token_use`` and ``exp`` from the verified API Gateway authorizer claims. The
token's ``client_id`` (the app client id) must equal the trusted audience the
deployment was configured with; any attempt to influence the audience, tenant, or
workspace through custom claims, the body, or headers is ignored.

These red-green tests pin that contract with production-shaped access-token claims
(a "payload v2" that matches the live authorizer context — ``client_id`` present,
no ``aud``, no ``custom:*``):

* a real bracketed ``[admin]`` access token dispatches (202);
* a real bracketed ``[users]`` access token is denied by the admin gate (403);
* a wrong app client is denied (403/401);
* an ID token (``token_use != access``) is denied (401);
* a request with no authorizer context is denied (401);
* audience/tenant/workspace injected via ``aud``/``custom:*``/body/headers are
  ignored — they never grant or change the server-owned binding.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timezone
from typing import Any

# Local modules
from operations.execute.dispatcher_handler import DispatcherRequestHandler

_EXP = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_STATE_MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:gbaw-executor"

# Server-owned deployment binding. The trusted audience is the Cognito app client
# id the access token presents in its ``client_id`` claim.
_TENANT = "tenant.default"
_WORKSPACE = "workspace.default"
_TRUSTED_CLIENT = "client.web-console"
_ADMIN_GROUP = "admin"


class _FakeSfn:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.starts.append(kwargs)
        return {"executionArn": "arn:aws:states:us-west-2:123456789012:execution:gbaw-executor:x"}


class _FakeStore:
    def __init__(self, *, state: str = "approved", tenant: str = _TENANT, workspace: str = _WORKSPACE) -> None:
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
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        trusted_audience=_TRUSTED_CLIENT,
        admin_group=_ADMIN_GROUP,
    )


def _production_claims(
    *,
    groups: object = "[admin]",
    client_id: str = _TRUSTED_CLIENT,
    token_use: str = "access",
) -> dict[str, Any]:
    """Production-shaped Cognito **access** token claims (payload v2).

    Matches what API Gateway's JWT authorizer forwards for a real access token:
    ``client_id`` present, NO ``aud``, NO ``custom:*`` tenant/workspace claims,
    and ``cognito:groups`` flattened to the bracketed string form.
    """
    return {
        "sub": "subject.admin-1",
        "client_id": client_id,
        "token_use": token_use,
        "exp": _EXP,
        "cognito:groups": groups,
        "scope": "operations/execute",
    }


def _event(
    *,
    claims: dict[str, Any] | None = None,
    with_authorizer: bool = True,
    body: Any = None,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_context: dict[str, Any] = {"http": {"method": "POST"}, "requestId": "req-1"}
    if with_authorizer:
        request_context["authorizer"] = {"jwt": {"claims": claims if claims is not None else _production_claims()}}
    event: dict[str, Any] = {"requestContext": request_context, "pathParameters": {"operationId": _OP}}
    if body is not None:
        event["body"] = body
    if headers is not None:
        event["headers"] = headers
    return event


def test_real_admin_access_token_dispatches() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event())
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"]) == {"operation_id": _OP, "state": "dispatched"}
    assert len(sfn.starts) == 1
    assert json.loads(sfn.starts[0]["input"]) == {"operation_id": _OP}


def test_real_users_access_token_denied_by_admin_gate() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(claims=_production_claims(groups="[users]")))
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error_code"] == "AUTHORIZATION_DENIED"
    assert sfn.starts == []


def test_wrong_app_client_denied() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(claims=_production_claims(client_id="client.attacker")))
    assert resp["statusCode"] in {401, 403}
    assert sfn.starts == []


def test_id_token_denied() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(claims=_production_claims(token_use="id")))
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error_code"] == "IDENTITY_CONTEXT_INVALID"
    assert sfn.starts == []


def test_no_authorizer_context_denied() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(with_authorizer=False))
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error_code"] == "IDENTITY_CONTEXT_INVALID"
    assert sfn.starts == []


def test_audience_injected_via_aud_claim_is_ignored() -> None:
    # An access token that (unusually) also carries an ``aud`` and matching
    # custom claims must not be able to steer the binding: the server-owned
    # config is authoritative and the token's own ``client_id`` still gates.
    sfn, store = _FakeSfn(), _FakeStore()
    claims = _production_claims()
    claims["aud"] = "attacker-audience"
    claims["custom:tenant_id"] = "tenant.attacker"
    claims["custom:workspace_id"] = "workspace.attacker"
    resp = _handler(sfn, store).handle(_event(claims=claims))
    # Still dispatches because tenant/workspace/audience are bound from config,
    # not from these claims.
    assert resp["statusCode"] == 202
    assert len(sfn.starts) == 1


def test_tenant_workspace_injected_via_body_and_headers_are_ignored() -> None:
    sfn, store = _FakeSfn(), _FakeStore()
    body = json.dumps(
        {
            "tenant_id": "tenant.attacker",
            "workspace_id": "workspace.attacker",
            "aud": "attacker-audience",
            "sub": "subject.attacker",
        }
    )
    headers = {
        "x-tenant-id": "tenant.attacker",
        "x-workspace-id": "workspace.attacker",
        "authorization": "Bearer forged",
    }
    resp = _handler(sfn, store).handle(_event(body=body, headers=headers))
    assert resp["statusCode"] == 202
    assert len(sfn.starts) == 1


def test_untrusted_client_even_with_admin_group_is_denied_before_dispatch() -> None:
    # A caller presenting the admin group but from an untrusted app client must
    # be denied at the identity/client boundary, never reaching the admin gate.
    sfn, store = _FakeSfn(), _FakeStore()
    resp = _handler(sfn, store).handle(_event(claims=_production_claims(groups="[admin]", client_id="client.attacker")))
    assert resp["statusCode"] in {401, 403}
    assert sfn.starts == []
