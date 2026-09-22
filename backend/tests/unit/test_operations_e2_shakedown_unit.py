"""Unit tests for the public-safe E2 deployed approval shakedown harness (#414).

Drives the ``operations.validation.e2_shakedown`` runner against an in-memory
fake HTTP transport modeling a deployed E2 control plane. The checks are
discriminating: each asserts one behavior of the deployed E2 boundary
(prepare -> self-approval denied -> distinct approver -> replay -> evidence ->
reject -> cancel -> expiry/race) and every check is bounded and leak-free. No
token is ever persisted or echoed into the emitted summary.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any, Optional

# Third-party packages
import pytest

# Local modules
from operations.validation import e2_shakedown as sd

pytestmark = pytest.mark.unit

ENDPOINT = "https://api.example.execute-api.us-west-2.amazonaws.com"
ACCESS = "access-token-value"
APPROVER = "approver-token-value"
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
OP_ID = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeDeployedE2:
    """A minimal in-memory model of the deployed E2 boundary over HTTP."""

    def __init__(self) -> None:
        self.state = "pending_approval"
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body) -> sd.HttpResponse:
        self.calls.append((method, url))
        auth = (headers or {}).get("Authorization", "")
        path = url[len(ENDPOINT) :]

        # Unauthenticated requests are denied at the gateway.
        if not auth:
            return _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID", "safe_message": "unauthenticated"})

        if method == "POST" and path == "/operations/prepare":
            payload = json.loads(body.decode("utf-8"))
            if "requester" in payload:  # injected identity -> rejected
                return _resp(400, {"error_code": "CONTRACT_INVALID", "safe_message": "invalid"})
            return _resp(
                201,
                {
                    "operation_id": OP_ID,
                    "decision": "approval_required",
                    "prepared_hash": "sha256:" + "a" * 64,
                    "persisted": True,
                    "replayed": False,
                },
            )

        if method == "POST" and path == f"/operations/{OP_ID}/approve":
            # The requester's own token is denied (self-approval); a distinct
            # approver token grants once, then conflicts.
            if ACCESS in auth:
                return _resp(403, {"error_code": "AUTHORIZATION_DENIED", "safe_message": "denied"})
            if self.state == "pending_approval":
                self.state = "approved"
                return _resp(200, {"approval_id": "approval.x", "decision": "granted"})
            return _resp(409, {"error_code": "STATE_CONFLICT", "safe_message": "conflict"})

        if method == "POST" and path == f"/operations/{OP_ID}/reject":
            return _resp(409, {"error_code": "STATE_CONFLICT", "safe_message": "already approved"})

        if method == "POST" and path == f"/operations/{OP_ID}/cancel":
            return _resp(409, {"error_code": "STATE_CONFLICT", "safe_message": "already approved"})

        if method == "GET" and path == f"/operations/{OP_ID}":
            return _resp(
                200,
                {"operation_id": OP_ID, "state": self.state, "handoff": {"executor_id": "executor.gamelift-capacity"}},
            )

        return _resp(404, {"error_code": "OPERATION_NOT_FOUND", "safe_message": "not found"})


def _resp(status: int, body: dict) -> sd.HttpResponse:
    return sd.HttpResponse(status=status, content_type="application/json", body_bytes=json.dumps(body).encode("utf-8"))


def _config() -> sd.E2ShakedownConfig:
    return sd.E2ShakedownConfig(
        endpoint=ENDPOINT,
        access_token=ACCESS,
        approver_token=APPROVER,
        fleet_id=FLEET,
        location=LOCATION,
        observation_id=OBS_ID,
    )


def test_shakedown_runs_full_flow_and_all_checks_pass() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeMultiOpDeployedE2())
    report = runner.run()
    assert report.ok, report
    names = {c.name for c in report.checks}
    # The acceptance flow checks are all present.
    assert {
        "prepare",
        "self_approval_denied",
        "distinct_approver_grants",
        "replay_grant_conflicts",
        "evidence",
        "reject_conflict",
        "cancel_conflict",
    } <= names


def test_unauthenticated_prepare_is_denied() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeDeployedE2())
    check = runner.check_unauthenticated_denied()
    assert check.passed


def test_summary_never_contains_tokens() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeMultiOpDeployedE2())
    report = runner.run()
    blob = json.dumps(report.to_summary())
    assert ACCESS not in blob
    assert APPROVER not in blob


def test_report_is_json_summarizable_and_bounded() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeMultiOpDeployedE2())
    report = runner.run()
    summary = report.to_summary()
    # The summary reduces each check to a bounded name/passed/detail, never a
    # raw response body.
    for entry in summary["checks"]:
        assert set(entry) <= {"name", "passed", "detail"}


# -- Independent-operation lifecycle + optional expiry (issue #414 E2) -------
#
# The single shared-op flow above proves grant + replay conflict. These drive
# the harness's INDEPENDENT-operation checks: each prepares its own operation
# (a fresh idempotency token) so a cancel/reject terminal transition on one
# never perturbs another, and proves the replayed terminal decision conflicts.


class FakeMultiOpDeployedE2:
    """A stateful multi-operation model keyed by idempotency token.

    Distinct prepares (distinct idempotency tokens) mint distinct ``op_`` ids
    with independent lifecycles, so the harness can prove requester cancellation
    and distinct-admin rejection on separate operations. Optionally supports a
    real-time expiry: an operation prepared while ``expiry_seconds`` is set flips
    to ``expired`` on the first access at/after its recorded due time.
    """

    def __init__(self, *, expiry_seconds: Optional[float] = None, clock: Optional[Any] = None) -> None:
        self._ops: dict[str, dict[str, Any]] = {}
        self._by_token: dict[str, str] = {}
        self._seq = 0
        self._expiry_seconds = expiry_seconds
        self._clock = clock or (lambda: 0.0)
        self.calls: list[tuple[str, str]] = []

    def _maybe_expire(self, op: dict[str, Any]) -> None:
        due = op.get("due_at")
        if due is not None and op["state"] in ("pending_approval", "prepared") and self._clock() >= due:
            op["state"] = "expired"

    def __call__(self, method, url, headers, body) -> sd.HttpResponse:
        self.calls.append((method, url))
        auth = (headers or {}).get("Authorization", "")
        path = url[len(ENDPOINT) :]
        if not auth:
            return _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID", "safe_message": "unauthenticated"})

        if method == "POST" and path == "/operations/prepare":
            payload = json.loads(body.decode("utf-8"))
            if "requester" in payload:
                return _resp(400, {"error_code": "CONTRACT_INVALID", "safe_message": "invalid"})
            token = payload.get("idempotency_token", "")
            if token in self._by_token:
                op_id = self._by_token[token]
                op = self._ops[op_id]
                return _resp(201, _prep_body(op_id, replayed=True))
            self._seq += 1
            op_id = f"op_{self._seq:026d}"[:29]
            op_id = "op_" + ("%026d" % self._seq)
            due = self._clock() + self._expiry_seconds if self._expiry_seconds is not None else None
            self._ops[op_id] = {"state": "pending_approval", "due_at": due, "approval": None}
            self._by_token[token] = op_id
            return _resp(201, _prep_body(op_id, replayed=False))

        op_id = _op_from_path(path)
        op = self._ops.get(op_id) if op_id else None
        if op is None:
            return _resp(404, {"error_code": "OPERATION_NOT_FOUND", "safe_message": "not found"})
        self._maybe_expire(op)

        if method == "GET":
            return _resp(200, {"operation_id": op_id, "state": op["state"], "approval": op["approval"]})

        if method == "POST" and path.endswith("/approve"):
            if op["state"] != "pending_approval":
                return _resp(409, {"error_code": "STATE_CONFLICT", "safe_message": "conflict"})
            if ACCESS in auth:  # self-approval
                return _resp(403, {"error_code": "AUTHORIZATION_DENIED", "safe_message": "denied"})
            op["state"] = "approved"
            op["approval"] = {"approval_id": "approval.x", "decision": "granted"}
            return _resp(200, op["approval"])

        if method == "POST" and path.endswith("/cancel"):
            if op["state"] != "pending_approval":
                return _resp(409, {"error_code": "STATE_CONFLICT", "safe_message": "conflict"})
            # The requester (access token) may cancel their own pending op.
            op["state"] = "cancelled"
            return _resp(200, {"new_state": "cancelled"})

        if method == "POST" and path.endswith("/reject"):
            if op["state"] != "pending_approval":
                return _resp(409, {"error_code": "STATE_CONFLICT", "safe_message": "conflict"})
            if ACCESS in auth:  # requester cannot reject
                return _resp(403, {"error_code": "AUTHORIZATION_DENIED", "safe_message": "denied"})
            op["state"] = "rejected"
            return _resp(200, {"new_state": "rejected"})

        return _resp(404, {"error_code": "OPERATION_NOT_FOUND", "safe_message": "not found"})


def _prep_body(op_id: str, *, replayed: bool) -> dict[str, Any]:
    return {
        "operation_id": op_id,
        "decision": "approval_required",
        "prepared_hash": "sha256:" + "a" * 64,
        "persisted": True,
        "replayed": replayed,
    }


def _op_from_path(path: str) -> Optional[str]:
    # /operations/{op}/action  or  /operations/{op}
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "operations" and parts[1].startswith("op_"):
        return parts[1]
    return None


def test_requester_cancel_and_replay_conflict_on_independent_op() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeMultiOpDeployedE2())
    check = runner.check_requester_cancel_and_replay_conflict()
    assert check.passed, check.detail


def test_distinct_admin_reject_and_replay_conflict_on_independent_op() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeMultiOpDeployedE2())
    check = runner.check_distinct_admin_reject_and_replay_conflict()
    assert check.passed, check.detail


def test_independent_ops_do_not_share_state() -> None:
    # Cancelling one op must not move another: the harness mints a fresh op per
    # check, so the grant flow and the cancel flow never collide.
    transport = FakeMultiOpDeployedE2()
    runner = sd.E2ShakedownRunner(config=_config(), transport=transport)
    report = runner.run()
    assert report.ok, report.to_summary()
    names = {c.name for c in report.checks}
    assert {"requester_cancel_and_replay_conflict", "distinct_admin_reject_and_replay_conflict"} <= names


def test_default_run_skips_realtime_expiry_and_stays_fast() -> None:
    # With no GBAW_E2_EXPIRY_WAIT_SECONDS the optional expiry check is not run.
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeMultiOpDeployedE2())
    report = runner.run()
    names = {c.name for c in report.checks}
    assert "realtime_expiry" not in names


def test_realtime_expiry_check_proves_expired_when_wait_configured() -> None:
    # When configured, the harness prepares a fresh op, waits the bounded
    # interval, and proves GET reads expired and approval conflicts. A fake clock
    # advances the transport so the unit test never actually sleeps.
    ticks = {"t": 0.0}
    transport = FakeMultiOpDeployedE2(expiry_seconds=2.0, clock=lambda: ticks["t"])
    config = sd.E2ShakedownConfig(
        endpoint=ENDPOINT,
        access_token=ACCESS,
        approver_token=APPROVER,
        fleet_id=FLEET,
        location=LOCATION,
        observation_id=OBS_ID,
        expiry_wait_seconds=2.0,
    )

    def _advance(seconds: float) -> None:
        ticks["t"] += seconds

    runner = sd.E2ShakedownRunner(config=config, transport=transport, sleeper=_advance)
    check = runner.check_realtime_expiry()
    assert check.passed, check.detail


def test_run_includes_realtime_expiry_when_configured() -> None:
    ticks = {"t": 0.0}
    transport = FakeMultiOpDeployedE2(expiry_seconds=2.0, clock=lambda: ticks["t"])
    config = sd.E2ShakedownConfig(
        endpoint=ENDPOINT,
        access_token=ACCESS,
        approver_token=APPROVER,
        fleet_id=FLEET,
        location=LOCATION,
        observation_id=OBS_ID,
        expiry_wait_seconds=2.0,
    )
    runner = sd.E2ShakedownRunner(
        config=config, transport=transport, sleeper=lambda s: ticks.__setitem__("t", ticks["t"] + s)
    )
    report = runner.run()
    names = {c.name for c in report.checks}
    assert "realtime_expiry" in names
    assert report.ok, report.to_summary()


# -- CLI env resolution for the optional expiry wait (issue #414) -----------


def test_cli_defaults_expiry_wait_to_zero_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GBAW_E2_ENDPOINT",
        "GBAW_E2_ACCESS_TOKEN",
        "GBAW_E2_APPROVER_TOKEN",
        "GBAW_E2_FLEET_ID",
        "GBAW_E2_LOCATION",
        "GBAW_E2_OBSERVATION_ID",
        "GBAW_E2_CURRENT_DESIRED",
        "GBAW_E2_EXPIRY_WAIT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    args = sd._parse_args(
        [
            "--endpoint",
            ENDPOINT,
            "--access-token",
            ACCESS,
            "--approver-token",
            APPROVER,
            "--fleet-id",
            FLEET,
            "--location",
            LOCATION,
            "--observation-id",
            OBS_ID,
        ]
    )
    config = sd.build_config_from_env(args)
    assert config.expiry_wait_seconds == 0.0


def test_cli_reads_expiry_wait_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GBAW_E2_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("GBAW_E2_ACCESS_TOKEN", ACCESS)
    monkeypatch.setenv("GBAW_E2_APPROVER_TOKEN", APPROVER)
    monkeypatch.setenv("GBAW_E2_FLEET_ID", FLEET)
    monkeypatch.setenv("GBAW_E2_LOCATION", LOCATION)
    monkeypatch.setenv("GBAW_E2_OBSERVATION_ID", OBS_ID)
    monkeypatch.setenv("GBAW_E2_EXPIRY_WAIT_SECONDS", "5")
    args = sd._parse_args([])
    config = sd.build_config_from_env(args)
    assert config.expiry_wait_seconds == 5.0


def test_config_rejects_unbounded_expiry_wait() -> None:
    with pytest.raises(ValueError):
        sd.E2ShakedownConfig(
            endpoint=ENDPOINT,
            access_token=ACCESS,
            approver_token=APPROVER,
            fleet_id=FLEET,
            location=LOCATION,
            observation_id=OBS_ID,
            expiry_wait_seconds=10_000.0,
        )
