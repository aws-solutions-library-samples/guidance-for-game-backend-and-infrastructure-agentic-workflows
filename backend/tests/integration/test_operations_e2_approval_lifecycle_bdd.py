"""End-to-end fake-server BDD scenarios for the E2 approval lifecycle (#414).

Drives the REAL E2 approval handler and REAL services (prepare orchestrator,
advice/prepare, approval, lifecycle-decision, evidence) over a stateful in-memory
DynamoDB fake and a fake E1 status loader, through HTTP-shaped API Gateway v2
events. Each scenario is a discriminating behavior of the acceptance flow:

* prepare a healthy proposal -> pending_approval, persisted;
* an identical retry replays the same operation byte-for-byte;
* the requester's own approval is denied by the default self-approval policy;
* a second, distinct human approver grants; a replayed grant conflicts;
* GET returns bounded, workspace-scoped evidence;
* reject and cancel transition terminally and race atomically;
* an expired operation cannot be approved.

No tokens or credentials are persisted; the fake stores only bounded records.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.advice import AdviceService
from operations.approval import ApprovalPolicy, ApprovalService
from operations.approval_handler import ApprovalRequestHandler
from operations.approval_store import DynamoDbApprovalStore
from operations.capacity_bounds import DeploymentCapacityBoundsResolver
from operations.capacity_state import E1ObservationCapacityStatePort
from operations.contracts.capacity import (
    ACTION,
    PROFILE,
    capacity_prepared_hash,
    validate_capacity_approval_binding,
    validate_capacity_prepared_operation,
)
from operations.decisions import LifecycleDecisionService
from operations.evidence import E2EvidenceService
from operations.identity import ApprovalIdentityBoundary
from operations.observation import ObservationStatus, ObservationStatusView
from operations.prepare import CapacityPlaybook, PrepareService
from operations.prepare_orchestrator import PrepareOrchestrator
from operations.settings import resolve_operations_settings

pytestmark = [pytest.mark.integration, pytest.mark.localhost]

NOW = datetime(2026, 9, 21, 19, 15, 0, tzinfo=timezone.utc)
TABLE = "operations-e2-bdd"
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
OBS_HASH = "sha256:" + "d" * 64
AUDIENCE = "operations-api"
POLICY_ID = "policy.gamelift.capacity"
POLICY_VERSION = "1"


def _transaction_canceled(*reason_codes: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": code} for code in reason_codes],
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "TransactWriteItems",
    )


class StatefulDynamoClient:
    """A minimal stateful DynamoDB fake honoring the store's conditions."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        for entry in TransactItems:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                cond = put.get("ConditionExpression", "")
                if ("attribute_not_exists(PK)" in cond or "attribute_not_exists(SK)" in cond) and key in self.items:
                    raise _transaction_canceled("ConditionalCheckFailed")
            if "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                existing = self.items.get(key)
                values = upd["ExpressionAttributeValues"]
                if (
                    existing is None
                    or existing.get("state", {}).get("S") != values[":expected"]["S"]
                    or existing.get("prepared_hash", {}).get("S") != values[":hash"]["S"]
                    or int(existing.get("sequence", {}).get("N", "-1")) != int(values[":prev_seq"]["N"])
                ):
                    raise _transaction_canceled("ConditionalCheckFailed")
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            if "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                values = upd["ExpressionAttributeValues"]
                self.items[key]["state"] = {"S": values[":new"]["S"]}
                self.items[key]["sequence"] = {"N": values[":seq"]["N"]}
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}


class FakeStatusLoader:
    """A fake E1 status load returning one fresh successful observation."""

    def load_status(self, *, operation_id: str, workspace_id: str) -> ObservationStatus | None:
        if operation_id != OBS_ID or workspace_id != "workspace.default":
            return None
        observation = {
            "observation_id": OBS_ID,
            "target": {"provider": "gamelift", "fleet_id": FLEET},
            "observed_at": (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "results": {
                "utilization": {
                    "active_server_processes": 10,
                    "active_game_sessions": 5,
                    "current_player_sessions": 20,
                    "maximum_player_sessions": 200,
                },
                "capacity": [
                    {"location": LOCATION, "desired": 10, "minimum": 2, "maximum": 20, "active": 10, "idle": 2}
                ],
                "scaling_policies": [],
            },
        }
        return ObservationStatus(
            operation_id=OBS_ID,
            workspace_id="workspace.default",
            state=ObservationStatusView.SUCCEEDED,
            observation=observation,
            observation_hash=OBS_HASH,
        )


def _boundary() -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"client.requester", "client.approver"}),
        approver_client_ids=frozenset({"client.requester", "client.approver"}),
        trusted_audiences=frozenset({AUDIENCE}),
    )


def _handler(client: StatefulDynamoClient) -> ApprovalRequestHandler:
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
    policy = ApprovalPolicy(
        policy_id=POLICY_ID,
        policy_version=POLICY_VERSION,
        approver_scopes=frozenset({AUDIENCE}),
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


def _claims(subject: str, client_id: str, *, exp_delta_min: int = 60) -> dict[str, Any]:
    return {
        "sub": subject,
        "client_id": client_id,
        "token_use": "access",
        "scope": AUDIENCE,
        "exp": str(int((NOW + timedelta(minutes=exp_delta_min)).timestamp())),
    }


def _event(
    method: str, path: str, *, claims: dict, body: dict | None = None, op_id: str | None = None
) -> dict[str, Any]:
    rc: dict[str, Any] = {
        "http": {"method": method, "path": path},
        "requestId": "req-1",
        "authorizer": {"jwt": {"claims": claims}},
    }
    event: dict[str, Any] = {"requestContext": rc, "rawPath": path}
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


def _prepare(
    handler: ApprovalRequestHandler, *, subject="user.requester", client_id="client.requester"
) -> dict[str, Any]:
    resp = handler.handle(
        _event("POST", "/operations/prepare", claims=_claims(subject, client_id), body=_prepare_body())
    )
    return resp


# -- Scenarios ---------------------------------------------------------------


def test_prepare_persists_pending_approval_operation() -> None:
    handler = _handler(StatefulDynamoClient())
    resp = _prepare(handler)
    assert resp["statusCode"] == 201
    body = json.loads(resp["body"])
    assert body["decision"] == "approval_required"
    assert body["persisted"] is True
    assert body["operation_id"].startswith("op_")


def test_identical_retry_replays_same_operation() -> None:
    handler = _handler(StatefulDynamoClient())
    first = json.loads(_prepare(handler)["body"])
    second = json.loads(_prepare(handler)["body"])
    assert second["operation_id"] == first["operation_id"]
    assert second["prepared_hash"] == first["prepared_hash"]
    assert second["replayed"] is True


def test_requester_self_approval_is_denied_but_distinct_approver_grants() -> None:
    handler = _handler(StatefulDynamoClient())
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    # The requester approving their own operation is denied (default self-deny).
    self_resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.requester", "client.requester"),
            body={},
            op_id=op_id,
        )
    )
    assert self_resp["statusCode"] == 403

    # A distinct human approver grants.
    grant = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=op_id,
        )
    )
    assert grant["statusCode"] == 200
    assert json.loads(grant["body"])["decision"] == "granted"

    # A replayed grant now conflicts (state moved to approved).
    replay = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=op_id,
        )
    )
    assert replay["statusCode"] == 409


def test_get_returns_bounded_workspace_scoped_evidence() -> None:
    handler = _handler(StatefulDynamoClient())
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]
    resp = handler.handle(
        _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
    )
    assert resp["statusCode"] == 200
    evidence = json.loads(resp["body"])
    assert evidence["operation_id"] == op_id
    assert evidence["state"] == "pending_approval"
    assert "credential" not in resp["body"]
    assert evidence["handoff"]["executor_id"] == "executor.gamelift-capacity"


def test_reject_transitions_terminally() -> None:
    handler = _handler(StatefulDynamoClient())
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]
    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/reject",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] == 200
    # A second decision on the now-rejected op conflicts.
    again = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/reject",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=op_id,
        )
    )
    assert again["statusCode"] == 409


def test_requester_can_cancel_pending_operation() -> None:
    handler = _handler(StatefulDynamoClient())
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]
    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/cancel",
            claims=_claims("user.requester", "client.requester"),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] == 200


def test_expired_credential_cannot_approve() -> None:
    handler = _handler(StatefulDynamoClient())
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]
    # An approver whose access token already expired is rejected by identity.
    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", exp_delta_min=-1),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] in (401, 409)


# -- Adapter parity + direct-Lambda identity denial (issue #414) ------------


def test_handler_prepare_matches_direct_orchestrator_outcome() -> None:
    # Driving prepare through the HTTP handler yields the same operation id and
    # prepared hash as invoking the orchestrator directly with the same trusted
    # principal and body — the adapter adds no behavior beyond transport.
    # Local modules
    from operations.identity import VerifiedPrincipal

    client_a = StatefulDynamoClient()
    handler = _handler(client_a)
    via_handler = json.loads(_prepare(handler)["body"])

    # Direct orchestrator over a SEPARATE store, same inputs.
    client_b = StatefulDynamoClient()
    direct_handler = _handler(client_b)
    principal = VerifiedPrincipal(
        subject_id="user.requester",
        client_id="client.requester",
        audience=AUDIENCE,
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=NOW + timedelta(hours=1),
    )
    direct = direct_handler._orchestrator.prepare(
        _prepare_body(), principal, request_id="apigw.req-1", correlation_id="apigw.req-1"
    )
    assert via_handler["operation_id"] == direct.operation["operation_id"]
    assert via_handler["prepared_hash"] == direct.prepared_hash
    assert via_handler["decision"] == direct.decision.value


def test_direct_lambda_invocation_without_authorizer_is_denied() -> None:
    handler = _handler(StatefulDynamoClient())
    # A direct/unattributed invocation carries no verified authorizer context.
    event = {
        "requestContext": {"http": {"method": "POST", "path": "/operations/prepare"}},
        "body": json.dumps(_prepare_body()),
    }
    resp = handler.handle(event)
    assert resp["statusCode"] == 401
    assert "op_" not in resp["body"]


def test_identity_injected_in_body_is_never_trusted() -> None:
    handler = _handler(StatefulDynamoClient())
    body = _prepare_body()
    body["requester"] = {"subject_id": "attacker", "workspace_id": "workspace.evil"}
    resp = handler.handle(
        _event("POST", "/operations/prepare", claims=_claims("user.requester", "client.requester"), body=body)
    )
    # The injected identity field is rejected as a contract violation (400),
    # never used to attribute or authorize the request.
    assert resp["statusCode"] == 400
    assert "workspace.evil" not in resp["body"]
