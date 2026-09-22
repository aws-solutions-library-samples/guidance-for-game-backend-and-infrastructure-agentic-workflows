"""Production-handler BDD: group-based approval over the API Gateway authorizer
string form (issue #414).

This drives the REAL E2 approval handler and REAL services (prepare
orchestrator, advice/prepare, approval, evidence) over the SAME proven stateful
in-memory DynamoDB fake and fake E1 status loader used by
``test_operations_e2_approval_lifecycle_bdd`` (imported here so the two BDDs
cannot drift from the store's real condition contract). The only difference is
that the approval policy authorizes by **group** (``admin``) rather than scope,
and the API Gateway JWT authorizer claims are shaped the way the HTTP API
(payload format 2.0) authorizer actually delivers a Cognito ``cognito:groups``
array: flattened into a *bracketed* string (``"[admin]"``, ``"[users]"``).

Before the #414 fix the handler's ``str.split()`` turned ``"[admin]"`` into the
single token ``"[admin]"``, which never intersected the required group
``"admin"`` — the live distinct-admin approver was denied with 403 even though
the decoded token genuinely carried ``groups=["admin"]``. These scenarios pin
the fixed behavior end to end:

* a distinct approver whose authorizer claim is ``"[admin]"`` **grants**;
* an approver whose claim is ``"[users]"`` is **denied** (not authorized);
* the requester approving their own operation is **denied** (self-approval);
* neither the request **body** nor a custom **header** can inject a group — the
  handler reads identity only from ``requestContext.authorizer.jwt.claims``.

References for the claim representation:
* Cognito groups are a JSON array claim —
  https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-user-groups.html
* API Gateway forwards verified JWT claims to the integration —
  https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html
"""

from __future__ import annotations

# Standard library
import json
from datetime import timedelta
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.advice import AdviceService
from operations.approval import ApprovalPolicy, ApprovalService
from operations.approval_handler import ApprovalRequestHandler
from operations.approval_store import DynamoDbApprovalStore
from operations.capacity_bounds import DeploymentCapacityBoundsResolver
from operations.capacity_state import E1ObservationCapacityStatePort
from operations.contracts.capacity import (
    PROFILE,
    capacity_prepared_hash,
    validate_capacity_approval_binding,
    validate_capacity_prepared_operation,
)
from operations.decisions import LifecycleDecisionService
from operations.evidence import E2EvidenceService
from operations.prepare import CapacityPlaybook, PrepareService
from operations.prepare_orchestrator import PrepareOrchestrator
from operations.settings import resolve_operations_settings

# Reuse the proven fakes/constants/boundary from the lifecycle BDD so the store
# contract cannot drift between the two suites.
from integration.test_operations_e2_approval_lifecycle_bdd import (
    AUDIENCE,
    FLEET,
    LOCATION,
    NOW,
    OBS_ID,
    POLICY_ID,
    POLICY_VERSION,
    TOKEN,
    FakeStatusLoader,
    StatefulDynamoClient,
    _boundary,
)

pytestmark = [pytest.mark.integration, pytest.mark.localhost]

TABLE = "operations-e2-group-bdd"
APPROVER_GROUP = "admin"


def _handler(client: StatefulDynamoClient) -> ApprovalRequestHandler:
    """Build the real E2 handler with a GROUP-authorizing approval policy."""
    ops = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "operate"})
    boundary = _boundary()
    store = DynamoDbApprovalStore(client=client, table_name=TABLE, clock=lambda: NOW)
    loader = FakeStatusLoader()

    def _advice_factory(observation_id: str) -> AdviceService:
        state_port = E1ObservationCapacityStatePort(
            status_loader=loader, observation_id=observation_id, clock=lambda: NOW
        )
        bounds_port = DeploymentCapacityBoundsResolver(
            state_port=state_port,
            floor=0,
            ceiling=1_000_000,
            max_step=1_000_000,
            enrollment_id="enrollment.gamelift.capacity",
            enrollment_version="1",
            policy_id=POLICY_ID,
            policy_version=POLICY_VERSION,
        )
        return AdviceService(
            settings=ops,
            identity_boundary=boundary,
            state_port=state_port,
            bounds_port=bounds_port,
            clock=lambda: NOW,
        )

    playbook = CapacityPlaybook(
        playbook_id="playbook.gamelift-capacity",
        playbook_version="1.0.0",
        playbook_hash="sha256:" + "0" * 64,
        profile=PROFILE,
        retry_policy={
            "max_attempts": 3,
            "base_delay_seconds": 2,
            "max_delay_seconds": 60,
            "reconcile_before_retry": True,
        },
        future_executor_binding={"executor_id": "executor.gamelift-capacity", "executor_binding_version": "1.0"},
    )
    orchestrator = PrepareOrchestrator(
        prepare_service=PrepareService(
            settings=ops,
            identity_boundary=boundary,
            playbook=playbook,
            clock=lambda: NOW,
            operation_ttl_seconds=ops.preparation_expiry_s,
        ),
        store=store,
        clock=lambda: NOW,
        deployment_mode="operate",
        advice_service_factory=_advice_factory,
        preparation_expiry_s=ops.preparation_expiry_s,
    )
    # Group-based policy: only members of the ``admin`` group may approve, and
    # self-approval is never allowed (no low-risk self-approval actions).
    policy = ApprovalPolicy(
        policy_id=POLICY_ID,
        policy_version=POLICY_VERSION,
        approver_groups=frozenset({APPROVER_GROUP}),
        low_risk_self_approval_actions=frozenset(),
    )
    return ApprovalRequestHandler(
        orchestrator=orchestrator,
        approval_service=ApprovalService(
            identity_boundary=boundary,
            policy=policy,
            store=store,
            clock=lambda: NOW,
            operation_validator=validate_capacity_prepared_operation,
            operation_hasher=capacity_prepared_hash,
            binding_validator=validate_capacity_approval_binding,
        ),
        decision_service=LifecycleDecisionService(
            identity_boundary=boundary, policy=policy, store=store, clock=lambda: NOW
        ),
        evidence_service=E2EvidenceService(store=store, identity_boundary=boundary),
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience=AUDIENCE,
    )


def _claims(subject: str, client_id: str, *, groups: object = None) -> dict[str, Any]:
    """Build authorizer claims. ``groups`` is passed through verbatim so a test
    can inject the exact bracketed string API Gateway delivers."""
    # Standard library
    claims: dict[str, Any] = {
        "sub": subject,
        "client_id": client_id,
        "token_use": "access",
        "scope": AUDIENCE,
        "exp": str(int((NOW + timedelta(minutes=60)).timestamp())),
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    return claims


def _event(
    method: str,
    path: str,
    *,
    claims: dict,
    body: dict | None = None,
    op_id: str | None = None,
    headers: dict | None = None,
) -> dict[str, Any]:
    rc: dict[str, Any] = {
        "http": {"method": method, "path": path},
        "requestId": "req-1",
        "authorizer": {"jwt": {"claims": claims}},
    }
    event: dict[str, Any] = {"requestContext": rc, "rawPath": path}
    if headers is not None:
        event["headers"] = headers
    if body is not None:
        event["body"] = json.dumps(body)
    if op_id is not None:
        event["pathParameters"] = {"operationId": op_id}
    return event


def _prepare_body(desired: int = 14) -> dict[str, Any]:
    return {
        "request_contract_version": "1.0",
        "capability_id": "gamelift.capacity-adjustment",
        "idempotency_token": TOKEN,
        "observation_id": OBS_ID,
        "proposal": {
            "fleet_id": FLEET,
            "location": LOCATION,
            "requested": {"desired": desired, "minimum": 2, "maximum": 20},
        },
    }


def _prepare(handler: ApprovalRequestHandler) -> str:
    """Prepare one pending-approval operation as the requester; return its id."""
    resp = handler.handle(
        _event(
            "POST",
            "/operations/prepare",
            claims=_claims("user.requester", "client.requester"),
            body=_prepare_body(),
        )
    )
    assert resp["statusCode"] in (200, 201), resp
    return json.loads(resp["body"])["operation_id"]


def test_distinct_admin_group_string_form_grants() -> None:
    """A distinct approver whose authorizer claim is the string ``"[admin]"``
    grants — the exact live-403 case, now green."""
    handler = _handler(StatefulDynamoClient())
    op_id = _prepare(handler)

    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", groups="[admin]"),
            op_id=op_id,
        )
    )

    assert resp["statusCode"] == 200, resp


def test_users_group_string_form_is_denied() -> None:
    """An approver whose claim is ``"[users]"`` is not in the ``admin`` group and
    is denied — proving the parser does not over-accept."""
    handler = _handler(StatefulDynamoClient())
    op_id = _prepare(handler)

    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", groups="[users]"),
            op_id=op_id,
        )
    )

    assert resp["statusCode"] != 200, resp


def test_requester_self_approval_still_denied_even_with_admin_group() -> None:
    """Self-approval is denied even when the requester carries ``"[admin]"`` —
    separation of duties is enforced independently of the group fix."""
    handler = _handler(StatefulDynamoClient())
    op_id = _prepare(handler)

    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.requester", "client.requester", groups="[admin]"),
            op_id=op_id,
        )
    )

    assert resp["statusCode"] != 200, resp


def test_group_cannot_be_injected_via_body() -> None:
    """A ``cognito:groups`` value in the request BODY must not authorize — the
    handler reads groups only from the authorizer claims."""
    handler = _handler(StatefulDynamoClient())
    op_id = _prepare(handler)

    # Approver has NO admin group in claims but tries to smuggle it in the body.
    poisoned_body = {"cognito:groups": "[admin]", "groups": ["admin"]}
    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", groups="[users]"),
            body=poisoned_body,
            op_id=op_id,
        )
    )

    assert resp["statusCode"] != 200, resp


def test_group_cannot_be_injected_via_header() -> None:
    """A ``cognito:groups`` value in a request HEADER must not authorize."""
    handler = _handler(StatefulDynamoClient())
    op_id = _prepare(handler)

    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", groups="[users]"),
            headers={"cognito:groups": "[admin]", "x-groups": "admin"},
            op_id=op_id,
        )
    )

    assert resp["statusCode"] != 200, resp
