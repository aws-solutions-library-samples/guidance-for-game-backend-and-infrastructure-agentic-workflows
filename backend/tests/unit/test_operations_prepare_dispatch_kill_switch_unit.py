"""Prepare + dispatch kill-switch enforcement tests (issue #416, E4).

The kill-switch is enforced at every lifecycle phase, not only in the executor:

* :class:`~operations.prepare.PrepareService` checks the ``prepare`` phase before
  it authorizes/binds an operation, so a disabled prepare phase fails closed with
  the bounded ``AUTHORIZATION_DENIED`` prepare boundary error and no operation is
  materialized.
* :class:`~operations.execute.dispatcher_handler.DispatcherRequestHandler` checks
  the ``dispatch`` phase before it starts the workflow, so a disabled dispatch
  phase returns a bounded 403 and no Step Functions execution is started.

Both gates are optional so the existing E2/E3 suites (no gate) are unchanged.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.kill_switch_gate import PhaseDenied

pytestmark = [pytest.mark.unit, pytest.mark.fast]


class _GateStub:
    def __init__(self, *, permit: bool) -> None:
        self._permit = permit
        self.phases: list[str] = []

    def require_phase(self, phase: str) -> Any:
        self.phases.append(phase)
        if not self._permit:
            raise PhaseDenied(phase, "disabled")
        return object()


# -- dispatch ---------------------------------------------------------------


def _dispatch_event() -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": "POST", "path": "/operations/op_" + "a" * 26 + "/dispatch"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "admin-1",
                        "client_id": "trusted-audience",
                        "token_use": "access",
                        "exp": "9999999999",
                        "cognito:groups": "[admin]",
                    }
                }
            },
        },
        "pathParameters": {"operationId": "op_" + "a" * 26},
    }


class _DispatchStore:
    def load_dispatch_view(self, operation_id: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "state": "approved",
            "tenant_id": "tenant-1",
            "workspace_id": "ws-1",
        }


class _Sfn:
    def __init__(self) -> None:
        self.starts = 0

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.starts += 1
        return {}


def _dispatcher(gate: Any) -> Any:
    # Local modules
    from operations.execute.dispatcher_handler import DispatcherRequestHandler

    return DispatcherRequestHandler(
        store=_DispatchStore(),
        step_functions=_Sfn(),
        state_machine_arn="arn:aws:states:us-west-2:123456789012:stateMachine:x",
        tenant_id="tenant-1",
        workspace_id="ws-1",
        trusted_audience="trusted-audience",
        admin_group="admin",
        kill_switch_gate=gate,
    )


def test_dispatch_denied_when_dispatch_phase_off() -> None:
    gate = _GateStub(permit=False)
    handler = _dispatcher(gate)
    handler._sfn = _Sfn()  # type: ignore[attr-defined]
    response = handler.handle(_dispatch_event())
    assert response["statusCode"] == 403
    assert "dispatch" in gate.phases


def test_dispatch_allowed_when_phase_on() -> None:
    gate = _GateStub(permit=True)
    handler = _dispatcher(gate)
    response = handler.handle(_dispatch_event())
    assert response["statusCode"] == 202
    assert "dispatch" in gate.phases


def test_dispatch_no_gate_preserves_behavior() -> None:
    handler = _dispatcher(gate=None)
    response = handler.handle(_dispatch_event())
    assert response["statusCode"] == 202


# -- prepare ----------------------------------------------------------------


def _prepare_service(gate):
    # Standard library
    from datetime import datetime, timezone

    # Local modules
    from operations.identity import ApprovalIdentityBoundary
    from operations.prepare import CapacityPlaybook, PrepareService
    from operations.settings import OperationsSettings

    settings = OperationsSettings(
        mode="remediate",
        per_read_budget_s=3.0,
        persistence_budget_s=3.0,
        cancellation_margin_s=3.0,
        observation_ttl_s=1800,
    )
    boundary = ApprovalIdentityBoundary(
        tenant_id="tenant-1",
        workspace_id="ws-1",
        requester_client_ids=frozenset({"client-1"}),
        approver_client_ids=frozenset({"client-1"}),
        trusted_audiences=frozenset({"aud-1"}),
    )
    # Local modules
    from operations.contracts.capacity import PROFILE

    playbook = CapacityPlaybook(
        playbook_id="pb-1",
        playbook_version="1.0",
        playbook_hash="sha256:" + "0" * 64,
        profile=PROFILE,
        retry_policy={"max_attempts": 1, "backoff_seconds": 1},
        future_executor_binding={"executor_id": "exec-1", "executor_binding_version": "1.0"},
    )
    return PrepareService(
        settings=settings,
        identity_boundary=boundary,
        playbook=playbook,
        clock=lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        kill_switch_gate=gate,
    )


def test_prepare_denied_when_prepare_phase_off() -> None:
    # Local modules
    from operations.prepare import PrepareBoundaryError, PrepareErrorCode

    gate = _GateStub(permit=False)
    service = _prepare_service(gate)

    class _Principal:
        pass

    # The prepare gate is checked before any advice work; a denied prepare phase
    # raises AUTHORIZATION_DENIED regardless of the (here minimal) inputs.
    with pytest.raises(PrepareBoundaryError) as excinfo:
        service.prepare({}, _ctx(), idempotency_token="idem_" + "a" * 20)
    assert excinfo.value.error_code is PrepareErrorCode.AUTHORIZATION_DENIED
    assert "prepare" in gate.phases


def _ctx():
    # Standard library
    from datetime import datetime, timezone

    # Local modules
    from operations.identity import VerifiedPrincipal
    from operations.prepare import PrepareRequestContext

    principal = VerifiedPrincipal(
        subject_id="user-1",
        client_id="client-1",
        audience="aud-1",
        tenant_id="tenant-1",
        workspace_id="ws-1",
        expires_at=datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc),
        groups=frozenset({"users"}),
        scopes=frozenset(),
    )
    return PrepareRequestContext(
        requester=principal,
        request_id="req-000001",
        correlation_id="cor-000001",
        deployment_mode="remediate",
        tenant_policy="remediate",
        workspace_policy="remediate",
        principal_authority="remediate",
        capability_maximum="remediate",
        operation_risk_policy="remediate",
    )
