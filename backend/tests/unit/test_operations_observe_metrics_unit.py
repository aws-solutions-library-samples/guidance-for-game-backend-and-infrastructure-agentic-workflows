"""Unit tests for the CloudWatch observation metrics sink (issue #413).

Asserts the four CloudWatch metric names are published **exactly** as
``ObservationFailures``/``ObservationTimeouts``/``StuckOperations``/
``ObservationRequestLatency`` under the configured namespace, and that no
dimension (workspace, fleet, account) leaks to CloudWatch.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.observe.metrics import (
    METRIC_FAILURES,
    METRIC_LATENCY,
    METRIC_STUCK,
    METRIC_TIMEOUTS,
    CloudWatchObservationMetrics,
)

NAMESPACE = "GBAW/Operations"


class FakeCloudWatch:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        return {}


def _sink() -> tuple[CloudWatchObservationMetrics, FakeCloudWatch]:
    client = FakeCloudWatch()
    return CloudWatchObservationMetrics(client=client, namespace=NAMESPACE), client


def _names(client: FakeCloudWatch) -> list[str]:
    return [put["MetricData"][0]["MetricName"] for put in client.puts]


def test_exact_metric_names() -> None:
    assert METRIC_FAILURES == "ObservationFailures"
    assert METRIC_TIMEOUTS == "ObservationTimeouts"
    assert METRIC_STUCK == "StuckOperations"
    assert METRIC_LATENCY == "ObservationRequestLatency"


def test_provider_failure_publishes_failures() -> None:
    sink, client = _sink()
    sink.record("observation.failed", 1.0, dimensions={"reason": "provider"})
    assert _names(client) == ["ObservationFailures"]
    assert client.puts[0]["Namespace"] == NAMESPACE


def test_deadline_failure_publishes_timeouts() -> None:
    sink, client = _sink()
    sink.record("observation.failed", 1.0, dimensions={"reason": "deadline"})
    assert _names(client) == ["ObservationTimeouts"]


@pytest.mark.parametrize("reason", ["not_found", "contract_invalid"])
def test_client_caused_failures_do_not_publish_failures(reason: str) -> None:
    # A caller's mistyped or unsupported fleet is a client error, not a service
    # fault. It must not increment ObservationFailures and so must not feed the
    # failures alarm; no CloudWatch metric is published for it.
    sink, client = _sink()
    sink.record("observation.failed", 1.0, dimensions={"reason": reason})
    assert client.puts == []


def test_explicit_timeout_event_publishes_timeouts() -> None:
    sink, client = _sink()
    sink.record("observation.timeout", 1.0, dimensions={"reason": "provider"})
    assert _names(client) == ["ObservationTimeouts"]


def test_in_progress_publishes_stuck_operations() -> None:
    sink, client = _sink()
    sink.record("observation.in_progress", 1.0)
    assert _names(client) == ["StuckOperations"]


def test_latency_publishes_milliseconds() -> None:
    sink, client = _sink()
    sink.put_latency_ms(403.194)
    assert _names(client) == ["ObservationRequestLatency"]
    assert client.puts[0]["MetricData"][0]["Unit"] == "Milliseconds"
    assert client.puts[0]["MetricData"][0]["Value"] == pytest.approx(403.194)


def test_latency_publish_is_skipped_when_too_little_time_remains() -> None:
    # When the request has already consumed the budget down to less than one
    # persistence call's worth of time, the end-to-end latency publish is
    # skipped so a slow put_metric_data cannot push the invocation past the
    # Lambda timeout. The skip is counted so it stays observable.
    client = FakeCloudWatch()
    sink = CloudWatchObservationMetrics(
        client=client, namespace=NAMESPACE, total_deadline_s=15.0, min_publish_remaining_s=3.0
    )
    # 13 s elapsed of a 15 s budget leaves 2 s < the 3 s floor: skip.
    sink.publish_latency(elapsed_s=13.0)
    assert client.puts == []
    assert sink.latency_skipped == 1


def test_latency_publish_runs_when_enough_time_remains() -> None:
    client = FakeCloudWatch()
    sink = CloudWatchObservationMetrics(
        client=client, namespace=NAMESPACE, total_deadline_s=15.0, min_publish_remaining_s=3.0
    )
    # 1 s elapsed leaves 14 s >= the 3 s floor: publish.
    sink.publish_latency(elapsed_s=1.0)
    assert _names(client) == ["ObservationRequestLatency"]
    assert sink.latency_skipped == 0


def test_recorded_and_replay_emit_no_named_metric() -> None:
    sink, client = _sink()
    sink.record("observation.recorded", 1.0, dimensions={"authority": "observe"})
    sink.record("observation.replay", 1.0)
    sink.record("observation.denied", 1.0, dimensions={"reason": "identity"})
    assert client.puts == []


def test_no_dimensions_are_forwarded_to_cloudwatch() -> None:
    sink, client = _sink()
    sink.record("observation.failed", 1.0, dimensions={"reason": "provider", "fleet_id": "fleet-secret"})
    metric = client.puts[0]["MetricData"][0]
    assert "Dimensions" not in metric


def test_namespace_required() -> None:
    with pytest.raises(ValueError):
        CloudWatchObservationMetrics(client=FakeCloudWatch(), namespace="  ")
