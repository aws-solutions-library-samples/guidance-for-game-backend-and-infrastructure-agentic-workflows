"""Production-bootstrap BDD scenarios for the E2 approval lifecycle (#414).

Unlike ``test_operations_e2_approval_lifecycle_bdd`` — which *manually*
constructs the E2 services and hand-injects the capacity-specific validator,
hasher, and binding into ``ApprovalService`` — these scenarios drive the REAL
deployable bootstrap. They build the handler and its ``DynamoDbApprovalStore``
exactly as ``operations.observe.lambda_entry._handler`` does on the host (over
injected fake AWS clients and the frozen environment contract), persist a real
capacity prepared operation through the bootstrap-built prepare path, and then
drive the acceptance flow end to end:

* the requester's own approval is denied by the default self-approval policy;
* a distinct admin-group approver grants;
* a replayed grant conflicts (the operation has moved to ``approved``);
* reject and cancel transition terminally and a second decision conflicts.

The distinction matters: the on-host handler wires ``ApprovalService`` with the
generic source-control validator/hasher unless the bootstrap injects the three
``operations.contracts.capacity`` functions. A manually constructed service
cannot catch that production wiring defect, so these scenarios exercise the
real bootstrap and would fail (self/distinct approval returning HTTP 400
``APPROVAL_INVALID``) if the injection regressed.

The bootstrap wires the E1 status port and approval clocks with the *real*
system clock (no clock injection point on the host), so freshness and token
timestamps here are derived from the real current time rather than a frozen
instant. No tokens or credentials are persisted; the fake stores only bounded
records.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
import operations.observe.lambda_entry as entry
from operations.observation import STATE_SUCCEEDED
from operations.observation_store import (
    _RESULT_SK,
    _STATE_SNAPSHOT_SK,
    _marshal,
)

pytestmark = [pytest.mark.integration, pytest.mark.localhost]

TABLE = "operations-observations"
AUDIENCE = "operations-api"
WORKSPACE = "workspace.default"
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Generous capacity bounds so an in-bounds proposal persists a pending approval
# (the server-owned default bounds of floor=0/ceiling=1/max_step=1 would deny
# the 10/2/20 fleet for BOUNDS_EXCEEDED before anything is persisted). max_step
# must not exceed the bound span (ceiling - floor).
_BOOTSTRAP_ENV = {
    "GBAW_OPERATIONS_MODE": "advise",
    "GBAW_OPERATIONS_TABLE_NAME": TABLE,
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
    "GBAW_OPERATIONS_WORKSPACE_ID": WORKSPACE,
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": AUDIENCE,
    "GBAW_OPERATIONS_CAPACITY_FLOOR": "1",
    "GBAW_OPERATIONS_CAPACITY_CEILING": "100",
    "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "99",
}


class StatefulDynamoClient:
    """A minimal stateful DynamoDB fake honoring the approval store's conditions.

    Mirrors the fake used by the manual E2 lifecycle BDD (transact writes with
    conditional puts and fenced updates) and adds ``put_item`` so the test can
    seed a real, production-marshalled SUCCEEDED E1 observation that the advise
    path reads to build a capacity proposal.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def _cancelled(self, *reason_codes: str) -> Exception:
        # Third-party packages
        from botocore.exceptions import ClientError

        return ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
                "CancellationReasons": [{"Code": code} for code in reason_codes],
                "ResponseMetadata": {"HTTPStatusCode": 400},
            },
            "TransactWriteItems",
        )

    def put_item(self, *, TableName: str, Item: dict[str, Any], **_: Any) -> dict[str, Any]:
        key = (Item["PK"]["S"], Item["SK"]["S"])
        self.items[key] = Item
        return {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        # All-or-nothing: evaluate every leg's condition first, apply none if any
        # fails. Update legs are dispatched by their OWN expressions rather than
        # assuming a state fence, so the E4 catalog Update leg (which binds
        # :cat_state / :cat_updated, never :expected) is handled correctly.
        for entry_ in TransactItems:
            if "Put" in entry_ and not _put_condition_holds(entry_["Put"], self.items):
                raise self._cancelled("ConditionalCheckFailed")
            if "Update" in entry_ and not _update_condition_holds(entry_["Update"], self.items):
                raise self._cancelled("ConditionalCheckFailed")
        for entry_ in TransactItems:
            if "Put" in entry_:
                item = entry_["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            if "Update" in entry_:
                _apply_update(entry_["Update"], self.items)
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}


class _FakeSession:
    """A boto3 session stand-in returning the one shared stateful client."""

    def __init__(self, client: StatefulDynamoClient) -> None:
        self._client = client

    def __call__(self, **_: Any) -> "_FakeSession":
        return self

    def client(self, name: str, **_: Any) -> Any:
        if name == "dynamodb":
            return self._client
        return object()


def _seed_succeeded_observation(client: StatefulDynamoClient, *, anchor: datetime) -> None:
    """Persist a production-marshalled SUCCEEDED E1 observation the advise reads."""
    # Local modules
    from operations.contracts import canonical_sha256

    observation = {
        "observation_id": OBS_ID,
        "target": {"provider": "gamelift", "fleet_id": FLEET},
        "observed_at": (anchor - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (anchor + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "results": {
            "utilization": {
                "active_server_processes": 10,
                "active_game_sessions": 5,
                "current_player_sessions": 20,
                "maximum_player_sessions": 200,
            },
            "capacity": [{"location": LOCATION, "desired": 10, "minimum": 2, "maximum": 20, "active": 10, "idle": 2}],
            "scaling_policies": [],
        },
    }
    observation_hash = canonical_sha256(dict(observation))
    op_pk = f"OP#{OBS_ID}"
    ttl = int((anchor + timedelta(hours=1)).timestamp())
    snapshot = {
        "PK": op_pk,
        "SK": _STATE_SNAPSHOT_SK,
        "record_type": "observation_state",
        "operation_id": OBS_ID,
        "workspace_id": WORKSPACE,
        "state": STATE_SUCCEEDED,
        "sequence": 1,
        "observation_hash": observation_hash,
        "ttl": ttl,
    }
    result = {
        "PK": op_pk,
        "SK": _RESULT_SK,
        "record_type": "observation_result",
        "operation_id": OBS_ID,
        "observation_hash": observation_hash,
        "observation_json": json.dumps(observation, separators=(",", ":"), sort_keys=True, ensure_ascii=False),
        "ttl": ttl,
    }
    client.put_item(TableName=TABLE, Item=_marshal(snapshot))
    client.put_item(TableName=TABLE, Item=_marshal(result))


def _build_bootstrap_router(monkeypatch: pytest.MonkeyPatch, *, anchor: datetime) -> Any:
    """Build the deployable handler exactly as the on-host bootstrap does."""
    client = StatefulDynamoClient()
    for key, value in _BOOTSTRAP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.Session", _FakeSession(client), raising=False)
    monkeypatch.setattr(entry, "_region", lambda: "us-west-2")
    entry._handler.cache_clear()
    router = entry._handler()
    entry._handler.cache_clear()
    _seed_succeeded_observation(client, anchor=anchor)
    return router


def _claims(subject: str, *, anchor: datetime, groups: str | None = None, exp_delta_min: int = 60) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": subject,
        "client_id": AUDIENCE,
        "token_use": "access",
        "scope": AUDIENCE,
        "exp": str(int((anchor + timedelta(minutes=exp_delta_min)).timestamp())),
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    return claims


def _event(method: str, path: str, *, claims: dict, body: dict | None = None, op_id: str | None = None) -> dict:
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


def _prepare(router: Any, *, anchor: datetime) -> dict[str, Any]:
    resp = router.handle(
        _event(
            "POST",
            "/operations/prepare",
            claims=_claims("user.requester", anchor=anchor),
            body=_prepare_body(),
        )
    )
    return resp


def test_bootstrap_persists_pending_capacity_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = _now()
    router = _build_bootstrap_router(monkeypatch, anchor=anchor)
    resp = _prepare(router, anchor=anchor)
    assert resp["statusCode"] == 201, resp["body"]
    body = json.loads(resp["body"])
    assert body["decision"] == "approval_required"
    assert body["persisted"] is True
    assert body["operation_id"].startswith("op_")


def test_bootstrap_self_approval_denied_then_distinct_admin_grants_then_replay_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _now()
    router = _build_bootstrap_router(monkeypatch, anchor=anchor)
    op_id = json.loads(_prepare(router, anchor=anchor)["body"])["operation_id"]

    # The requester approving their own operation is denied by the default
    # self-approval policy (403), NOT rejected as an invalid stored operation
    # (which is what generic-validator misconfiguration produced live: 400).
    self_resp = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.requester", anchor=anchor, groups="admin"),
            body={},
            op_id=op_id,
        )
    )
    assert self_resp["statusCode"] == 403, self_resp["body"]
    assert "APPROVAL_INVALID" not in self_resp["body"]

    # A distinct admin-group approver grants — this exercises the capacity
    # validator, hasher, and binding through the real bootstrap wiring.
    grant = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", anchor=anchor, groups="admin"),
            body={},
            op_id=op_id,
        )
    )
    assert grant["statusCode"] == 200, grant["body"]
    assert json.loads(grant["body"])["decision"] == "granted"

    # A replayed grant now conflicts (state moved to approved).
    replay = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", anchor=anchor, groups="admin"),
            body={},
            op_id=op_id,
        )
    )
    assert replay["statusCode"] == 409


def test_bootstrap_reject_transitions_terminally_then_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = _now()
    router = _build_bootstrap_router(monkeypatch, anchor=anchor)
    op_id = json.loads(_prepare(router, anchor=anchor)["body"])["operation_id"]
    resp = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/reject",
            claims=_claims("user.approver", anchor=anchor, groups="admin"),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] == 200, resp["body"]
    again = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/reject",
            claims=_claims("user.approver", anchor=anchor, groups="admin"),
            body={},
            op_id=op_id,
        )
    )
    assert again["statusCode"] == 409


def test_bootstrap_cancel_transitions_terminally_then_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = _now()
    router = _build_bootstrap_router(monkeypatch, anchor=anchor)
    op_id = json.loads(_prepare(router, anchor=anchor)["body"])["operation_id"]
    resp = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/cancel",
            claims=_claims("user.requester", anchor=anchor),
            body={},
            op_id=op_id,
        )
    )
    assert resp["statusCode"] == 200, resp["body"]
    again = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/cancel",
            claims=_claims("user.requester", anchor=anchor),
            body={},
            op_id=op_id,
        )
    )
    assert again["statusCode"] == 409


# -- Lazy-on-access expiry through the real bootstrap (issue #414) ----------


class _AdvanceableClock:
    """A clock the test moves forward to cross the operation's expires_at."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def set(self, at: datetime) -> None:
        self._at = at

    def __call__(self) -> datetime:
        return self._at


def _build_bootstrap_router_with_clock(
    monkeypatch: pytest.MonkeyPatch, *, clock: _AdvanceableClock, anchor: datetime
) -> Any:
    """Build the deployable handler, driving every bootstrap clock off ``clock``.

    The on-host bootstrap has no clock injection point; it reads ``entry._utcnow``
    everywhere (prepare/advice/approval/decision). Patching that single source
    lets the test anchor prepare and then advance past the prepared operation's
    expires_at to exercise the real bootstrap-wired lazy expiry.
    """
    client = StatefulDynamoClient()
    for key, value in _BOOTSTRAP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.Session", _FakeSession(client), raising=False)
    monkeypatch.setattr(entry, "_region", lambda: "us-west-2")
    monkeypatch.setattr(entry, "_utcnow", clock)
    entry._handler.cache_clear()
    router = entry._handler()
    entry._handler.cache_clear()
    _seed_succeeded_observation(client, anchor=anchor)
    return router


def test_bootstrap_get_after_due_transitions_to_expired_and_approval_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anchor at the real current time so the E1 freshness check (which the
    # bootstrap wires with the real system clock) accepts the seeded observation.
    anchor = _now()
    clock = _AdvanceableClock(anchor)
    router = _build_bootstrap_router_with_clock(monkeypatch, clock=clock, anchor=anchor)
    op_id = json.loads(_prepare(router, anchor=anchor)["body"])["operation_id"]

    # Advance the whole bootstrap clock past preparation_expiry_s (default 900s).
    due = anchor + timedelta(minutes=16)
    clock.set(due)

    # GET first transitions the due operation to expired, then surfaces it.
    evidence_resp = router.handle(
        _event(
            "GET",
            f"/operations/{op_id}",
            claims=_claims("user.viewer", anchor=due, groups="admin"),
            op_id=op_id,
        )
    )
    assert evidence_resp["statusCode"] == 200, evidence_resp["body"]
    evidence = json.loads(evidence_resp["body"])
    assert evidence["state"] == "expired"
    assert evidence["approval"] is None

    # A distinct admin approval after due must NOT grant — it conflicts (409),
    # and no grant is recorded.
    approve_resp = router.handle(
        _event(
            "POST",
            f"/operations/{op_id}/approve",
            claims=_claims("user.approver", anchor=due, groups="admin"),
            body={},
            op_id=op_id,
        )
    )
    assert approve_resp["statusCode"] == 409, approve_resp["body"]
    assert '"decision": "granted"' not in approve_resp["body"]


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
