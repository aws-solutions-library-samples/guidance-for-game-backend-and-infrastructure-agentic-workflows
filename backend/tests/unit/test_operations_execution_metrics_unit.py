"""E3 execution metrics sink tests (#415).

The execution service emits bounded, public-safe metric events; this sink
translates them into the named CloudWatch metrics the deployment monitors and
publishes NOTHING but the bounded metric name and value — never an account id,
ARN, fleet id, workspace id, or capacity value.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.metrics import (
    METRIC_EXECUTION_FAILURES,
    METRIC_HUMAN_RECONCILIATION,
    METRIC_LATENCY,
    METRIC_PROVIDER_WRITES,
    METRIC_RECONCILED,
    CloudWatchExecutionMetrics,
)


class _FakeCloudWatch:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        return {}


def _sink(cw: _FakeCloudWatch) -> CloudWatchExecutionMetrics:
    return CloudWatchExecutionMetrics(client=cw, namespace="GBAW/Operations")


def _names(cw: _FakeCloudWatch) -> list[str]:
    return [d["MetricName"] for put in cw.puts for d in put["MetricData"]]


def test_failure_event_maps_to_failure_metric() -> None:
    cw = _FakeCloudWatch()
    _sink(cw).record("execution.failed")
    assert _names(cw) == [METRIC_EXECUTION_FAILURES]


def test_human_reconciliation_event_maps_to_metric() -> None:
    cw = _FakeCloudWatch()
    _sink(cw).record("execution.human_reconciliation_required")
    assert _names(cw) == [METRIC_HUMAN_RECONCILIATION]


def test_reconciled_and_write_events_map() -> None:
    cw = _FakeCloudWatch()
    sink = _sink(cw)
    sink.record("execution.reconciled")
    sink.record("execution.provider_write_issued")
    assert _names(cw) == [METRIC_RECONCILED, METRIC_PROVIDER_WRITES]


def test_latency_is_published_in_milliseconds() -> None:
    cw = _FakeCloudWatch()
    _sink(cw).put_latency_ms(12.5)
    data = cw.puts[0]["MetricData"][0]
    assert data["MetricName"] == METRIC_LATENCY
    assert data["Unit"] == "Milliseconds"
    assert data["Value"] == 12.5


def test_no_dimensions_leak_to_cloudwatch() -> None:
    cw = _FakeCloudWatch()
    _sink(cw).record("execution.failed")
    for put in cw.puts:
        for datum in put["MetricData"]:
            assert "Dimensions" not in datum


def test_namespace_must_be_non_empty() -> None:
    with pytest.raises(ValueError):
        CloudWatchExecutionMetrics(client=_FakeCloudWatch(), namespace="  ")
