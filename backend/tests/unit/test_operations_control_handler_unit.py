"""Admin control HTTP handler tests (issue #416, E4).

:class:`~operations.control.control_handler.ControlRequestHandler` is the
authenticated HTTP boundary that applies an admin kill-switch control. Like the
E1/E2/E3 handlers it trusts identity ONLY from the API Gateway JWT authorizer
context — never the request body, headers, or path — and it binds the audience,
tenant, and workspace from server-owned config. The acting admin's identity
never appears in the request or response body; it is resolved from the verified
principal and handed to the control service (which records it only in the audit
store).

Contract:
* 401 when the authorizer context is absent/invalid.
* 403 when the caller is not the trusted app client, is not an admin, or the
  control service denies on authority.
* 409 on a compare-and-set version conflict.
* 200 with the applied config_version on success.
* 400 when the body is malformed or attempts to carry identity.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.control_audit_store import ControlCommitOutcome
from operations.control.control_handler import ControlRequestHandler
from operations.contracts.control_plane import CAPABILITY_ID

pytestmark = [pytest.mark.unit, pytest.mark.fast]


class _FakeControlService:
    def __init__(self, response: dict[str, Any] | None = None, *, error: Exception | None = None) -> None:
        self._response = response or {
            "contract_version": "1.0",
            "outcome": "applied",
            "config_version": 2,
            "reason_code": "APPLIED",
        }
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def apply(self, *, request: dict[str, Any], principal: Any, current_document: Any = None) -> dict[str, Any]:
        self.calls.append({"request": request, "principal": principal})
        if self._error is not None:
            raise self._error
        return self._response


def _desired(enabled: bool = True) -> dict[str, Any]:
    return {
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": enabled, "dispatch": enabled, "execute": enabled}},
    }


def _event(*, groups: str = "[admin]", client_id: str = "trusted-audience", body: Any = None) -> dict[str, Any]:
    if body is None:
        body = {"contract_version": "1.0", "expected_config_version": 1, "desired": _desired()}
    return {
        "requestContext": {
            "http": {"method": "POST", "path": "/operations/control"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "admin-1",
                        "client_id": client_id,
                        "token_use": "access",
                        "exp": "9999999999",
                        "cognito:groups": groups,
                    }
                }
            },
        },
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }


def _handler(service: Any) -> ControlRequestHandler:
    return ControlRequestHandler(
        control_service=service,
        tenant_id="tenant-1",
        workspace_id="ws-1",
        trusted_audience="trusted-audience",
        admin_group="admin",
    )


def _status(response: dict[str, Any]) -> int:
    return response["statusCode"]


def test_applied_returns_200() -> None:
    service = _FakeControlService()
    response = _handler(service).handle(_event())
    assert _status(response) == 200
    body = json.loads(response["body"])
    assert body["outcome"] == "applied"
    assert body["config_version"] == 2


def test_missing_authorizer_returns_401() -> None:
    response = _handler(_FakeControlService()).handle(
        {"requestContext": {"http": {"method": "POST", "path": "/operations/control"}}, "body": "{}"}
    )
    assert _status(response) == 401


def test_non_admin_returns_403() -> None:
    service = _FakeControlService()
    response = _handler(service).handle(_event(groups="[users]"))
    assert _status(response) == 403
    assert not service.calls  # never reaches the service


def test_wrong_client_returns_403() -> None:
    service = _FakeControlService()
    response = _handler(service).handle(_event(client_id="other-client"))
    assert _status(response) == 403
    assert not service.calls


def test_version_conflict_returns_409() -> None:
    service = _FakeControlService(
        {
            "contract_version": "1.0",
            "outcome": "version_conflict",
            "config_version": 5,
            "reason_code": "VERSION_CONFLICT",
        }
    )
    response = _handler(service).handle(_event())
    assert _status(response) == 409
    assert json.loads(response["body"])["config_version"] == 5


def test_identity_in_body_is_rejected_400() -> None:
    # The body must carry ONLY desired + expected_config_version; an actor/identity
    # field is a malformed request (additionalProperties: false) and is refused.
    bad = {
        "contract_version": "1.0",
        "expected_config_version": 1,
        "desired": _desired(),
        "actor": {"subject_id": "attacker"},
    }
    service = _FakeControlService()
    response = _handler(service).handle(_event(body=bad))
    assert _status(response) == 400
    assert not service.calls


def test_malformed_body_returns_400() -> None:
    service = _FakeControlService()
    response = _handler(service).handle(_event(body="not json{{"))
    assert _status(response) == 400
    assert not service.calls


def test_response_body_carries_no_identity() -> None:
    service = _FakeControlService()
    response = _handler(service).handle(_event())
    blob = response["body"]
    for identity in ("admin-1", "subject_id", "actor", "cognito"):
        assert identity not in blob
    # The service received the verified principal (identity out of band).
    principal = service.calls[0]["principal"]
    assert principal.subject_id == "admin-1"
    # The request handed to the service carries no identity field.
    assert "actor" not in service.calls[0]["request"]


def test_authority_denied_maps_to_403() -> None:
    from operations.control.control_service import ControlServiceError

    service = _FakeControlService(error=ControlServiceError("AUTHORIZATION_DENIED", "denied"))
    response = _handler(service).handle(_event())
    assert _status(response) == 403
