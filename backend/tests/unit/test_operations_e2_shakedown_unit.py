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
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeDeployedE2())
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
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeDeployedE2())
    report = runner.run()
    blob = json.dumps(report.to_summary())
    assert ACCESS not in blob
    assert APPROVER not in blob


def test_report_is_json_summarizable_and_bounded() -> None:
    runner = sd.E2ShakedownRunner(config=_config(), transport=FakeDeployedE2())
    report = runner.run()
    summary = report.to_summary()
    # The summary reduces each check to a bounded name/passed/detail, never a
    # raw response body.
    for entry in summary["checks"]:
        assert set(entry) <= {"name", "passed", "detail"}
