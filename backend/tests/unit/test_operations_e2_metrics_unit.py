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
