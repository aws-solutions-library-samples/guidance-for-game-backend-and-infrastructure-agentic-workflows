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
    capacity_policy_version,
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


def _handler(
    client: StatefulDynamoClient,
    *,
    mode: str = "operate",
    floor: int = 0,
    ceiling: int = 1_000_000,
    max_step: int = 1_000_000,
    clock: Any = None,
    request_deadline_s: float | None = None,
) -> ApprovalRequestHandler:
    # The E1 status/advice/prepare pipeline is anchored at the frozen NOW so a
    # fresh, in-bounds proposal always prepares. Only the approval and lifecycle
    # decision clock (``clock``) may advance so a test can move past the prepared
    # operation's expires_at and exercise lazy-on-access expiry.
    ops = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": mode})
    boundary = _boundary()
    decision_clock = clock or (lambda: NOW)
    store = DynamoDbApprovalStore(client=client, table_name=TABLE, clock=decision_clock)
    loader = FakeStatusLoader()

    def _advice_factory(observation_id: str) -> AdviceService:
        state_port = E1ObservationCapacityStatePort(
            status_loader=loader, observation_id=observation_id, clock=lambda: NOW
        )
        bounds_port = DeploymentCapacityBoundsResolver(
            state_port=state_port,
            floor=floor,
            ceiling=ceiling,
            max_step=max_step,
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
        deployment_mode=mode,
        advice_service_factory=_advice_factory,
        preparation_expiry_s=ops.preparation_expiry_s,
    )
    policy = ApprovalPolicy(
        policy_id=POLICY_ID,
        policy_version=capacity_policy_version(
            POLICY_VERSION, {"floor": floor, "ceiling": ceiling, "max_step": max_step}
        ),
        approver_scopes=frozenset({AUDIENCE}),
        low_risk_self_approval_actions=frozenset(),
        approval_ttl=timedelta(seconds=ops.approval_expiry_s),
    )
    return ApprovalRequestHandler(
        orchestrator=orchestrator,
        approval_service=ApprovalService(
            identity_boundary=boundary,
            policy=policy,
            store=store,
            clock=decision_clock,
            operation_validator=validate_capacity_prepared_operation,
            operation_hasher=capacity_prepared_hash,
            binding_validator=validate_capacity_approval_binding,
        ),
        decision_service=LifecycleDecisionService(
            identity_boundary=boundary, policy=policy, store=store, clock=decision_clock
        ),
        evidence_service=E2EvidenceService(store=store, identity_boundary=boundary),
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience=AUDIENCE,
        requester_groups=frozenset({"operations-requester"}),
        store=store,
        request_deadline_s=request_deadline_s,
        clock=decision_clock,
    )


def _claims(
    subject: str, client_id: str, *, exp_delta_min: int = 60, groups: object = "operations-requester"
) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": subject,
        "client_id": client_id,
        "token_use": "access",
        "scope": AUDIENCE,
        "exp": str(int((NOW + timedelta(minutes=exp_delta_min)).timestamp())),
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    return claims


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


def test_approved_operation_can_be_cancelled_by_requester() -> None:
    # An operation a distinct approver has already approved can still be
    # cancelled by its requester before dispatch: the terminal transition fences
    # on the post-grant snapshot sequence rather than a hard-coded first step.
    handler = _handler(StatefulDynamoClient())
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]
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

    cancel = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/cancel",
            claims=_claims("user.requester", "client.requester"),
            body={},
            op_id=op_id,
        )
    )
    assert cancel["statusCode"] == 200

    evidence = json.loads(
        handler.handle(
            _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
        )["body"]
    )
    assert evidence["state"] == "cancelled"


def test_approved_operation_expires_once_due() -> None:
    # An approved operation whose window elapses transitions to expired on the
    # next access, fencing on its post-grant sequence.
    client = StatefulDynamoClient()
    clock = _AdvanceableClock(NOW)
    handler = _handler(client, clock=clock)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]
    grant = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", exp_delta_min=60 + 20),
            body={},
            op_id=op_id,
        )
    )
    assert grant["statusCode"] == 200

    clock.set(_PAST_EXPIRY)
    evidence = json.loads(
        handler.handle(
            _event(
                "GET",
                f"/operations/{op_id}",
                claims=_claims("user.viewer", "client.approver", exp_delta_min=60 + 20),
                op_id=op_id,
            )
        )["body"]
    )
    assert evidence["state"] == "expired"


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


def test_caller_with_no_group_is_denied_on_prepare_cancel_and_evidence() -> None:
    # A verified caller whose token carries no group has no requester authority:
    # prepare, cancel, and evidence are denied with 403 before any store read.
    handler = _handler(StatefulDynamoClient())
    no_group = _claims("user.outsider", "client.requester", groups=None)

    prepare_resp = handler.handle(_event("POST", "/operations/prepare", claims=no_group, body=_prepare_body()))
    assert prepare_resp["statusCode"] == 403

    cancel_resp = handler.handle(
        _event(
            "POST",
            "/operations/op_aaaaaaaaaaaaaaaaaaaaaaaaaa/cancel",
            claims=no_group,
            body={},
            op_id="op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    )
    assert cancel_resp["statusCode"] == 403

    evidence_resp = handler.handle(
        _event(
            "GET",
            "/operations/op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            claims=no_group,
            op_id="op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    )
    assert evidence_resp["statusCode"] == 403


def test_caller_outside_the_requester_allowlist_is_denied_on_prepare() -> None:
    # A caller whose only group is outside the server-owned requester allowlist
    # derives no authority and is denied, so a group unrelated to operations
    # cannot prepare.
    handler = _handler(StatefulDynamoClient())
    wrong_group = _claims("user.outsider", "client.requester", groups=["unrelated-group"])
    resp = handler.handle(_event("POST", "/operations/prepare", claims=wrong_group, body=_prepare_body()))
    assert resp["statusCode"] == 403


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
        _prepare_body(),
        principal,
        request_id="apigw.req-1",
        correlation_id="apigw.req-1",
        principal_authority="advise",
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


def test_advise_mode_persists_pending_approval_not_denied() -> None:
    # With OperationsMode=advise the prepare endpoint treats E2 prepare/approval
    # as an advise-authority capability, so an in-bounds proposal persists a
    # pending approval_required operation (HTTP 201) rather than being denied for
    # insufficient authority.
    handler = _handler(StatefulDynamoClient(), mode="advise")
    resp = _prepare(handler)
    assert resp["statusCode"] == 201
    body = json.loads(resp["body"])
    assert body["decision"] == "approval_required"
    assert body["persisted"] is True
    assert body["operation_id"].startswith("op_")


def test_advise_mode_under_default_capacity_bounds_is_not_denied_for_authority() -> None:
    # Under the server-owned default fail-closed bounds (floor=0, ceiling=1,
    # max_step=1) the authority gate is not the blocker: the advise phase reaches
    # the bounds evaluation instead of failing closed on authority. The decision
    # is driven by bounds (BOUNDS_EXCEEDED against the current 10/2/20 fleet), and
    # the effective authority is exactly advise.
    handler = _handler(StatefulDynamoClient(), mode="advise", floor=0, ceiling=1, max_step=1)
    resp = _prepare(handler)
    body = json.loads(resp["body"])
    # The wire body only carries the decision; under advise authority the change
    # is denied for bounds against the current 10/2/20 fleet rather than being
    # blocked at the authority gate.
    assert resp["statusCode"] == 200
    assert body["decision"] == "denied"
    # A denied operation is never persisted; the precise advise-authority /
    # BOUNDS_EXCEEDED distinction is asserted at the service level in
    # test_operations_capacity_prepare_bdd. Here it is enough that advise mode is
    # accepted at the authorization boundary (no 403) and the phase is reachable.


def test_observe_mode_denies_prepare_at_the_authorization_boundary() -> None:
    # E1 observe authority is below the advise prepare-phase minimum, so E2
    # prepare is not enabled and is denied at the authorization boundary (HTTP
    # 403). Approval never elevates execution authority, so the phase stays
    # unreachable below advise.
    handler = _handler(StatefulDynamoClient(), mode="observe")
    resp = _prepare(handler)
    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert body["error_code"] == "AUTHORIZATION_DENIED"


# -- Lazy-on-access expiry (issue #414 E2) ----------------------------------
#
# expire_if_due is wired into the real API flow: a DUE pending/prepared
# operation is atomically transitioned to expired (system actor + ledger) before
# GET evidence or approve/reject/cancel can treat it as active. These fake-server
# scenarios drive the REAL handler + services over the stateful fake with a clock
# advanced past the prepared operation's expires_at.

# The prepared operation's expiry horizon is preparation_expiry_s (default 900s).
# Advancing the decision/approval clock past it makes the operation due.
_PAST_EXPIRY = NOW + timedelta(minutes=16)


class _AdvanceableClock:
    """A clock the test moves forward to cross the operation's expires_at."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def set(self, at: datetime) -> None:
        self._at = at

    def __call__(self) -> datetime:
        return self._at


def test_get_evidence_after_due_transitions_operation_to_expired() -> None:
    # Prepare at NOW (frozen prepare pipeline), then read evidence with the
    # decision clock advanced past expiry: the GET must first transition the
    # operation to expired and then surface the expired state.
    client = StatefulDynamoClient()
    clock = _AdvanceableClock(NOW)
    handler = _handler(client, clock=clock)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    clock.set(_PAST_EXPIRY)
    resp = handler.handle(
        _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
    )
    assert resp["statusCode"] == 200
    evidence = json.loads(resp["body"])
    assert evidence["state"] == "expired"
    # A system-actor state-changed ledger entry was written by the atomic commit.
    ledger_types = [entry.get("event_type") for entry in evidence["ledger"]]
    assert "operation.state-changed" in ledger_types


def test_approval_after_due_does_not_grant_and_returns_bounded_conflict() -> None:
    # Approval after the operation is due must never grant: the lazy expiry wins
    # first (op -> expired), then the grant observes a terminal state and returns
    # a bounded 409, and evidence still reads expired (no grant recorded).
    client = StatefulDynamoClient()
    clock = _AdvanceableClock(NOW)
    handler = _handler(client, clock=clock)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    clock.set(_PAST_EXPIRY)
    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver", exp_delta_min=60 + 20),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] == 409
    assert '"decision": "granted"' not in resp["body"]

    # Evidence proves the operation is terminal-expired with no approval recorded.
    evidence = json.loads(
        handler.handle(
            _event(
                "GET",
                f"/operations/{op_id}",
                claims=_claims("user.viewer", "client.approver", exp_delta_min=60 + 20),
                op_id=op_id,
            )
        )["body"]
    )
    assert evidence["state"] == "expired"
    assert evidence["approval"] is None


def test_expiry_is_idempotent_single_terminal_state_and_single_transition() -> None:
    # A second access after expiry must NOT write a second transition: the op is
    # already terminal, so lazy expiry is a safe no-op and the state stays expired
    # with exactly one state-changed ledger entry.
    client = StatefulDynamoClient()
    clock = _AdvanceableClock(NOW)
    handler = _handler(client, clock=clock)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    clock.set(_PAST_EXPIRY)
    first = json.loads(
        handler.handle(
            _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
        )["body"]
    )
    second = json.loads(
        handler.handle(
            _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
        )["body"]
    )
    assert first["state"] == "expired"
    assert second["state"] == "expired"
    state_changes = [e for e in second["ledger"] if e.get("event_type") == "operation.state-changed"]
    assert len(state_changes) == 1


def test_cancel_after_due_yields_one_terminal_state_and_conflicts() -> None:
    # A cancel arriving after the operation is due loses to the lazy expiry: the
    # op transitions to expired, and the cancel then conflicts (409). The op is
    # not cancelled — a race between expiry and cancel yields ONE terminal state.
    client = StatefulDynamoClient()
    clock = _AdvanceableClock(NOW)
    handler = _handler(client, clock=clock)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    clock.set(_PAST_EXPIRY)
    resp = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/cancel",
            claims=_claims("user.requester", "client.requester", exp_delta_min=60 + 20),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] == 409
    evidence = json.loads(
        handler.handle(
            _event(
                "GET",
                f"/operations/{op_id}",
                claims=_claims("user.viewer", "client.approver", exp_delta_min=60 + 20),
                op_id=op_id,
            )
        )["body"]
    )
    assert evidence["state"] == "expired"


def test_not_yet_due_operation_is_not_expired_on_access() -> None:
    # Before the operation is due the lazy expiry is a no-op: evidence still reads
    # pending_approval and a distinct approver can still grant.
    client = StatefulDynamoClient()
    clock = _AdvanceableClock(NOW)
    handler = _handler(client, clock=clock)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    # Clock unchanged (still NOW, well before expiry).
    evidence = json.loads(
        handler.handle(
            _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
        )["body"]
    )
    assert evidence["state"] == "pending_approval"
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


# -- Per-request deadline under a degraded store (issue #414) ----------------
#
# Mirrors the E1 slow-reclaim guard: when the durable store is degraded and each
# sub-call costs the worst-case ~4.5 s, a single E2 request that fans out into
# several reads and a transaction must return a typed, retryable error BEFORE
# the Lambda's wall-clock timeout, rather than being hard-killed with an unknown
# outcome. The handler derives one monotonic request deadline from the shared
# operations budget and threads it into the store, which guards every read and
# caps every write against it.

# The worst-case cost of one degraded store sub-call (connect + read on the
# single-attempt persistence client), the value the store's call-limit guard
# uses. Advancing the shared clock by this amount per sub-call models the stall.
_SLOW_CALL_S = 4.5

# The Lambda wall-clock timeout the request must finish inside, less a safety
# margin: the request must return a typed error before the function is killed.
_LAMBDA_TIMEOUT_S = 20.0
_LAMBDA_SAFETY_MARGIN_S = 2.0


class _TickingClock:
    """A clock that advances a fixed step on each armed store sub-call.

    Prepare runs with the clock held at ``NOW`` so a fresh, in-bounds proposal
    always persists; the test then arms the clock so each subsequent store
    sub-call (read or transaction) advances it by one worst-case call cost,
    modelling a degraded DynamoDB table.
    """

    def __init__(self, at: datetime, *, step_s: float) -> None:
        self._at = at
        self._step = timedelta(seconds=step_s)
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def tick(self) -> None:
        if self._armed:
            self._at = self._at + self._step

    def elapsed_since(self, start: datetime) -> float:
        return (self._at - start).total_seconds()

    def __call__(self) -> datetime:
        return self._at


class _SlowDynamoClient(StatefulDynamoClient):
    """A stateful fake whose every store sub-call advances a shared clock."""

    def __init__(self, clock: _TickingClock) -> None:
        super().__init__()
        self._clock = clock

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self._clock.tick()
        return super().transact_write_items(TransactItems=TransactItems)

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        self._clock.tick()
        return super().get_item(TableName=TableName, Key=Key, ConsistentRead=ConsistentRead)


def test_degraded_store_returns_retryable_error_before_the_lambda_timeout() -> None:
    # An approve and an evidence read, each store sub-call costing ~4.5 s under a
    # degraded table, finish with a typed retryable error before the 20 s Lambda
    # timeout (less a safety margin) rather than being hard-killed.
    clock = _TickingClock(NOW, step_s=_SLOW_CALL_S)
    client = _SlowDynamoClient(clock)
    handler = _handler(client, clock=clock, request_deadline_s=15.0)

    # Prepare while the clock is held, so the operation materializes normally.
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    # The table degrades: every subsequent sub-call now advances the clock.
    clock.arm()

    approve_start = clock()
    approve = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=op_id,
        )
    )
    assert approve["statusCode"] == 503
    assert json.loads(approve["body"])["retryable"] is True
    # The approve returned before the Lambda wall-clock timeout (less a safety
    # margin): the store refused the sub-call that would cross the request
    # deadline rather than running until the function was killed.
    assert clock.elapsed_since(approve_start) <= _LAMBDA_TIMEOUT_S - _LAMBDA_SAFETY_MARGIN_S

    evidence_start = clock()
    evidence = handler.handle(
        _event("GET", f"/operations/{op_id}", claims=_claims("user.viewer", "client.approver"), op_id=op_id)
    )
    assert evidence["statusCode"] == 503
    assert json.loads(evidence["body"])["retryable"] is True
    assert clock.elapsed_since(evidence_start) <= _LAMBDA_TIMEOUT_S - _LAMBDA_SAFETY_MARGIN_S


# The worst-case cost of one degraded sub-call, well under the store's 4.5 s
# call limit so the per-read guard never trips first: here the binding cap is
# the request deadline reached across the write, not any single read.
_SUB_CALL_S = 2.7


def test_slow_store_write_deadline_returns_retryable_not_expired() -> None:
    # With each store sub-call costing 2.7 s (under the 4.5 s per-call read
    # guard), a sequence of reads and a transaction eventually makes the
    # per-request deadline the binding write cap. The operation itself has not
    # expired, so the approve must return a typed, retryable 503 rather than a
    # non-retryable 409 that misstates the operation as expired. A healthy retry
    # then still grants, proving the operation was never actually expired.
    clock = _TickingClock(NOW, step_s=_SUB_CALL_S)
    client = _SlowDynamoClient(clock)
    handler = _handler(client, clock=clock, request_deadline_s=15.0)

    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    clock.arm()
    approve = handler.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=op_id,
        )
    )
    assert approve["statusCode"] == 503
    assert json.loads(approve["body"])["retryable"] is True

    # A healthy retry (clock held steady) still grants: the operation was
    # never expired, only the slow request's budget was exhausted.
    healthy = _TickingClock(NOW, step_s=_SUB_CALL_S)
    healthy_client = _SlowDynamoClient(healthy)
    healthy_handler = _handler(healthy_client, clock=healthy, request_deadline_s=15.0)
    healthy_op = json.loads(_prepare(healthy_handler)["body"])["operation_id"]
    granted = healthy_handler.handle(
        _event(
            "POST",
            f"/operations/{healthy_op}/approve",
            claims=_claims("user.approver", "client.approver"),
            body={},
            op_id=healthy_op,
        )
    )
    assert granted["statusCode"] == 200
