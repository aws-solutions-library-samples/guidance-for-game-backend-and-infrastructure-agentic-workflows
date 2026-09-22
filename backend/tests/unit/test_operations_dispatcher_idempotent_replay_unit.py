"""Dispatcher idempotent-replay tests (#415, E3 execute).

The dispatcher starts a Step Functions **Standard** execution with a stable,
deterministic execution name derived from the operation id. The independent
review flagged that the handler did not actually treat the Standard
``ExecutionAlreadyExists`` condition as an idempotent dispatch replay — it fell
into the generic catch-all and surfaced a 500. For Standard workflows a
same-name start of the *same* input is Step Functions' idempotency signal, so a
duplicate dispatch must be reported as an accepted ``dispatched`` replay, not a
failure.

These red-green tests pin that behavior: an ``ExecutionAlreadyExists`` from
``start_execution`` yields the same accepted (202) ``dispatched`` acknowledgement
as the first dispatch, while a genuine service fault still surfaces as a 500.
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


def _client_error(code: str) -> Exception:
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "raw provider text must not leak"}}, "StartExecution")


class _AlreadyExistsSfn:
    def __init__(self, code: str = "ExecutionAlreadyExists") -> None:
        self.starts: list[dict[str, Any]] = []
        self._code = code

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.starts.append(kwargs)
        raise _client_error(self._code)


class _FaultSfn:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.starts.append(kwargs)
        raise _client_error("StateMachineDoesNotExist")


class _FakeStore:
    def load_dispatch_view(self, operation_id: str) -> dict[str, Any] | None:
        if operation_id != _OP:
            return None
        return {
            "operation_id": operation_id,
            "state": "approved",
            "tenant_id": "tenant.default",
            "workspace_id": "workspace.default",
        }


def _handler(sfn: Any) -> DispatcherRequestHandler:
    return DispatcherRequestHandler(
        store=_FakeStore(),
        step_functions=sfn,
        state_machine_arn=_STATE_MACHINE_ARN,
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience="aud-client",
        admin_group="admin",
    )


def _event() -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "subject.admin-1",
                        "client_id": "client.web-console",
                        "token_use": "access",
                        "aud": "aud-client",
                        "exp": _EXP,
                        "cognito:groups": "[admin]",
                        "scope": "operations/execute",
                        "custom:tenant_id": "tenant.default",
                        "custom:workspace_id": "workspace.default",
                    }
                }
            },
        },
        "pathParameters": {"operationId": _OP},
    }


def test_execution_already_exists_is_idempotent_dispatch_replay() -> None:
    sfn = _AlreadyExistsSfn()
    resp = _handler(sfn).handle(_event())
    # Same accepted, typed acknowledgement as a first-time dispatch.
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body == {"operation_id": _OP, "state": "dispatched"}
    # Exactly one start was attempted; no second blind start.
    assert len(sfn.starts) == 1


def test_execution_already_exists_full_exception_name_is_idempotent() -> None:
    # boto3 sometimes surfaces the modeled exception name as the code.
    sfn = _AlreadyExistsSfn(code="ExecutionAlreadyExistsException")
    resp = _handler(sfn).handle(_event())
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"]) == {"operation_id": _OP, "state": "dispatched"}


def test_genuine_sfn_fault_still_fails_closed_without_leak() -> None:
    sfn = _FaultSfn()
    resp = _handler(sfn).handle(_event())
    assert resp["statusCode"] == 500
    body = json.loads(resp["body"])
    assert "raw provider text" not in resp["body"]
    assert body["error_code"] == "INTERNAL_ERROR"
