"""E4 control-plane metrics tests (issue #416).

:class:`~operations.control.metrics.CloudWatchControlMetrics` translates bounded,
public-safe control-plane events into named CloudWatch metrics. As with the E3
executor metrics, NO dimension ever crosses to CloudWatch — only the bounded
metric name and value — so an account id, ARN, workspace id, or config value can
never leak through a metric.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.metrics import (
    METRIC_CONTROL_APPLIED,
    METRIC_CONTROL_DENIED,
    METRIC_CONTROL_PUBLICATION_RECONCILED,
    METRIC_CONTROL_VERSION_CONFLICT,
    METRIC_EXPIRY_SWEEP_EXPIRED,
    METRIC_KILL_SWITCH_UNAVAILABLE,
    CloudWatchControlMetrics,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]


class _FakeCloudWatch:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        return {}


def _metrics(client: Any) -> CloudWatchControlMetrics:
    return CloudWatchControlMetrics(client=client, namespace="GameAgent/Operations")


def test_records_named_events() -> None:
    client = _FakeCloudWatch()
    metrics = _metrics(client)
    metrics.record("control.applied")
    metrics.record("control.version_conflict")
    metrics.record("control.denied")
    metrics.record("control.publication_reconciled")
    metrics.record("kill_switch.unavailable")
    names = [put["MetricData"][0]["MetricName"] for put in client.puts]
    assert names == [
        METRIC_CONTROL_APPLIED,
        METRIC_CONTROL_VERSION_CONFLICT,
        METRIC_CONTROL_DENIED,
        METRIC_CONTROL_PUBLICATION_RECONCILED,
        METRIC_KILL_SWITCH_UNAVAILABLE,
    ]


def test_publication_reconciled_is_a_bounded_count_metric() -> None:
    # Lost-response reconciliation must be observable: the event maps to a single
    # bounded Count metric with value 1.0 and no dimensions.
    client = _FakeCloudWatch()
    _metrics(client).record("control.publication_reconciled")
    assert len(client.puts) == 1
    datum = client.puts[0]["MetricData"][0]
    assert datum["MetricName"] == METRIC_CONTROL_PUBLICATION_RECONCILED
    assert datum["Value"] == 1.0
    assert datum["Unit"] == "Count"
    assert "Dimensions" not in datum
    assert METRIC_CONTROL_PUBLICATION_RECONCILED == "ControlPublicationReconciled"


def test_unknown_event_is_ignored() -> None:
    client = _FakeCloudWatch()
    _metrics(client).record("control.unknown")
    assert client.puts == []


def test_expiry_sweep_count_is_published() -> None:
    client = _FakeCloudWatch()
    _metrics(client).put_expiry_sweep_expired(7)
    put = client.puts[0]
    assert put["MetricData"][0]["MetricName"] == METRIC_EXPIRY_SWEEP_EXPIRED
    assert put["MetricData"][0]["Value"] == 7.0


def test_no_dimensions_ever_cross_to_cloudwatch() -> None:
    client = _FakeCloudWatch()
    _metrics(client).record("control.applied", dimensions={"workspace_id": "ws-1"})
    put = client.puts[0]
    assert "Dimensions" not in put["MetricData"][0]
