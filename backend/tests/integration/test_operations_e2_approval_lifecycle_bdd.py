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
        # All-or-nothing: evaluate every leg's condition first, apply none if any
        # fails. Update legs are dispatched by their OWN expressions rather than
        # assuming a state fence, so the E4 catalog Update leg (which binds
        # :cat_state / :cat_updated, never :expected) is handled correctly.
        for entry in TransactItems:
            if "Put" in entry and not _put_condition_holds(entry["Put"], self.items):
                raise _transaction_canceled("ConditionalCheckFailed")
            if "Update" in entry and not _update_condition_holds(entry["Update"], self.items):
                raise _transaction_canceled("ConditionalCheckFailed")
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            if "Update" in entry:
                _apply_update(entry["Update"], self.items)
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


def test_terminal_grant_advances_the_workspace_catalog_projection() -> None:
    """A terminal grant applies the E4 catalog Update leg, not just tolerates it.

    The commit's TransactWriteItems carries the state fence Update AND a second
    workspace-catalog Update leg (binding :cat_state / :cat_updated). This proves
    the catalog projection row's state advances to ``approved`` on a grant — i.e.
    the fake dispatched the catalog leg by its own expression and applied it,
    rather than crashing on the fence-only ``:expected`` assumption.
    """
    client = StatefulDynamoClient()
    handler = _handler(client)
    op_id = json.loads(_prepare(handler)["body"])["operation_id"]

    catalog_key = ("WS#workspace.default#CATALOG", f"OP#{op_id}")
    # At prepare, the catalog row exists and is pending_approval.
    assert client.items[catalog_key]["state"]["S"] == "pending_approval"

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
    # The catalog projection advanced via the dispatched catalog Update leg.
    assert client.items[catalog_key]["state"]["S"] == "approved"
    # updated_at was refreshed by the same leg's SET assignment.
    assert "updated_at" in client.items[catalog_key]


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


def test_advise_mode_persists_pending_approval_not_denied() -> None:
    # Live-response regression (#414): with OperationsMode=advise the prepare
    # endpoint previously returned HTTP 200 decision=denied for INSUFFICIENT_
    # AUTHORITY, making the whole E2 phase unreachable. E2 prepare/approval is an
    # advise-authority capability, so an in-bounds proposal must now persist a
    # pending approval_required operation (HTTP 201) instead of being denied.
    handler = _handler(StatefulDynamoClient(), mode="advise")
    resp = _prepare(handler)
    assert resp["statusCode"] == 201
    body = json.loads(resp["body"])
    assert body["decision"] == "approval_required"
    assert body["persisted"] is True
    assert body["operation_id"].startswith("op_")


def test_advise_mode_under_default_capacity_bounds_is_not_denied_for_authority() -> None:
    # Under the server-owned default fail-closed bounds (floor=0, ceiling=1,
    # max_step=1) the authority gate must no longer be the blocker: the advise
    # phase reaches the bounds evaluation instead of failing closed on authority.
    # The decision is therefore driven by bounds (BOUNDS_EXCEEDED against the
    # current 10/2/20 fleet), never by the old INSUFFICIENT_AUTHORITY denial that
    # made E2 unreachable, and the effective authority is exactly advise.
    handler = _handler(StatefulDynamoClient(), mode="advise", floor=0, ceiling=1, max_step=1)
    resp = _prepare(handler)
    body = json.loads(resp["body"])
    # The wire body only carries the decision; under advise authority the change
    # is denied for bounds against the current 10/2/20 fleet rather than being
    # blocked at the authority gate (the old INSUFFICIENT_AUTHORITY denial).
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


# -- generic transact-leg dispatch (issue #416, E4 catalog Update leg) --------

_SET_ASSIGNMENT = __import__("re").compile(r"([#\w]+)\s*=\s*(:[\w]+)")


def _set_clause(expression: str) -> str:
    # Standard library
    import re as _re

    match = _re.search(
        r"\bSET\b(.*?)(?:\bREMOVE\b|\bADD\b|\bDELETE\b|$)",
        expression,
        flags=_re.IGNORECASE | _re.DOTALL,
    )
    return match.group(1) if match else ""


def _put_condition_holds(put: dict, items: dict) -> bool:
    item = put["Item"]
    key = (item["PK"]["S"], item["SK"]["S"])
    cond = put.get("ConditionExpression", "")
    if ("attribute_not_exists(PK)" in cond or "attribute_not_exists(SK)" in cond) and key in items:
        return False
    if "attribute_exists(SK)" in cond and key not in items:
        return False
    return True


def _update_condition_holds(upd: dict, items: dict) -> bool:
    key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
    existing = items.get(key)
    cond = upd.get("ConditionExpression", "")
    names = upd.get("ExpressionAttributeNames", {})
    values = upd.get("ExpressionAttributeValues", {})
    if "attribute_exists" in cond and existing is None:
        return False
    # Enforce every "<attr> = :value" equality the condition declares, using only
    # the placeholders this leg actually binds (the catalog leg binds none of the
    # fence placeholders, so it is checked purely on existence).
    for name_token, value_token in _SET_ASSIGNMENT.findall(cond):
        if value_token not in values:
            continue
        attr = names.get(name_token, name_token)
        if existing is None or existing.get(attr) != values[value_token]:
            return False
    return True


def _apply_update(upd: dict, items: dict) -> None:
    key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
    existing = dict(items.get(key, {"PK": upd["Key"]["PK"], "SK": upd["Key"]["SK"]}))
    names = upd.get("ExpressionAttributeNames", {})
    values = upd.get("ExpressionAttributeValues", {})
    for name_token, value_token in _SET_ASSIGNMENT.findall(_set_clause(upd.get("UpdateExpression", ""))):
        if value_token not in values:
            continue
        attr = names.get(name_token, name_token)
        existing[attr] = values[value_token]
    items[key] = existing
