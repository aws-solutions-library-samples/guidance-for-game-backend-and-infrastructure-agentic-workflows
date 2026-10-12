"""Unit tests for the E2 approval CloudWatch metrics sink (issue #414).

The E2 approval surface publishes exactly four bounded, public-safe CloudWatch
metrics, named exactly, with NO dimensions or identifiers:

* ``PreparationFailures`` — a prepare request failed (non-conflict or conflict).
* ``ApprovalFailures`` — an approve/reject failed for any non-expiry reason.
* ``ApprovalExpired`` — an approve/reject/expiry failed because the operation or
  approval had expired.
* ``CancellationConflicts`` — a cancel (or any terminal decision) lost a fenced
  conditional race (a state conflict).
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.observe.e2_metrics import (
    METRIC_APPROVAL_EXPIRED,
    METRIC_APPROVAL_FAILURES,
    METRIC_CANCELLATION_CONFLICTS,
    METRIC_PREPARATION_FAILURES,
    CloudWatchApprovalMetrics,
)

pytestmark = pytest.mark.unit


class FakeCloudWatch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {}


def _sink(client: FakeCloudWatch) -> CloudWatchApprovalMetrics:
    return CloudWatchApprovalMetrics(client=client, namespace="GBAW/Operations")


def test_preparation_failure_publishes_named_metric_without_dimensions() -> None:
    client = FakeCloudWatch()
    _sink(client).record("preparation.failed")
    assert len(client.calls) == 1
    data = client.calls[0]["MetricData"][0]
    assert data["MetricName"] == METRIC_PREPARATION_FAILURES
    assert data["Value"] == 1.0
    assert "Dimensions" not in data


def test_approval_failure_and_expiry_split_correctly() -> None:
    client = FakeCloudWatch()
    sink = _sink(client)
    sink.record("approval.failed")
    sink.record("approval.expired")
    names = [c["MetricData"][0]["MetricName"] for c in client.calls]
    assert names == [METRIC_APPROVAL_FAILURES, METRIC_APPROVAL_EXPIRED]


def test_cancellation_conflict_publishes_conflict_metric() -> None:
    client = FakeCloudWatch()
    _sink(client).record("cancellation.conflict")
    assert client.calls[0]["MetricData"][0]["MetricName"] == METRIC_CANCELLATION_CONFLICTS


def test_unknown_event_publishes_nothing() -> None:
    client = FakeCloudWatch()
    _sink(client).record("something.else")
    assert client.calls == []


def test_namespace_is_used_and_no_identifier_dimensions_ever() -> None:
    client = FakeCloudWatch()
    sink = _sink(client)
    for event in ("preparation.failed", "approval.failed", "approval.expired", "cancellation.conflict"):
        sink.record(event)
    for call in client.calls:
        assert call["Namespace"] == "GBAW/Operations"
        for datum in call["MetricData"]:
            assert "Dimensions" not in datum


# --------------------------------------------------------------------------- #
# A shakedown-like sequence of expected caller outcomes must publish NO failure
# metric through the REAL sink, so a clean shakedown run never trips the paging
# PreparationFailures / ApprovalFailures alarms (issue #414).
# --------------------------------------------------------------------------- #


def test_shakedown_like_sequence_publishes_no_failure_metric_through_the_real_sink() -> None:
    # Drive the real handler through the expected 4xx outcomes a shakedown emits
    # (identity-injection rejection on prepare, self-approval denial, an unknown
    # operation on approve) with the REAL CloudWatch sink. None of them is a
    # service-side failure, so neither PreparationFailures nor ApprovalFailures
    # is published to CloudWatch, so a clean shakedown never pages.
    # Standard library
    import json
    from datetime import datetime, timedelta, timezone

    # Local modules
    from operations.approval import ApprovalBoundaryError, ApprovalErrorCode
    from operations.approval_handler import ApprovalRequestHandler
    from operations.prepare_orchestrator import PrepareOrchestratorError

    now = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)
    operation_id = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"

    class _Orchestrator:
        def prepare(self, body, requester, *, request_id, correlation_id, principal_authority="advise"):
            # A prepare body carrying an injected identity field is a caller
            # contract violation (400), not a service failure.
            raise PrepareOrchestratorError("contract_invalid", "prepare request is invalid")

    class _Approval:
        def grant(self, request, context):
            # Self-approval is denied (403) — an expected caller outcome.
            raise ApprovalBoundaryError(ApprovalErrorCode.AUTHORIZATION_DENIED, "self-approval denied")

    class _Decision:
        def reject(self, request, context):
            raise ApprovalBoundaryError(ApprovalErrorCode.OPERATION_NOT_FOUND, "unknown")

        def cancel(self, request, context):
            raise ApprovalBoundaryError(ApprovalErrorCode.STATE_CONFLICT, "conflict")

        def expire_if_due(self, request):
            return None

    class _Evidence:
        def load_evidence(self, *, operation_id, requester):
            return None

    client = FakeCloudWatch()
    handler = ApprovalRequestHandler(
        orchestrator=_Orchestrator(),
        approval_service=_Approval(),
        decision_service=_Decision(),
        evidence_service=_Evidence(),
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience="operations-api",
        requester_groups=frozenset({"operations-requester"}),
        metrics=_sink(client),
    )

    def _claims():
        return {
            "sub": "user.requester",
            "client_id": "client.requester",
            "token_use": "access",
            "cognito:groups": "operations-requester",
            "exp": str(int((now + timedelta(hours=1)).timestamp())),
        }

    def _event(method, path, *, op_id=None, body=None):
        rc = {
            "http": {"method": method, "path": path},
            "requestId": "req-1",
            "authorizer": {"jwt": {"claims": _claims()}},
        }
        event = {"requestContext": rc, "rawPath": path}
        if body is not None:
            event["body"] = json.dumps(body)
        if op_id is not None:
            event["pathParameters"] = {"operationId": op_id}
        return event

    handler.handle(_event("POST", "/operations/prepare", body={"requester": "x"}))
    handler.handle(_event("POST", f"/operations/{operation_id}/approve", op_id=operation_id, body={}))
    handler.handle(_event("POST", f"/operations/{operation_id}/reject", op_id=operation_id, body={}))

    published = [c["MetricData"][0]["MetricName"] for c in client.calls]
    assert METRIC_PREPARATION_FAILURES not in published
    assert METRIC_APPROVAL_FAILURES not in published
