"""Unit tests for the CloudWatch observation metrics sink (issue #413).

Asserts the four CloudWatch metric names are published **exactly** as
``ObservationFailures``/``ObservationTimeouts``/``StuckOperations``/
``ObservationRequestLatency`` under the configured namespace, and that no
dimension (workspace, fleet, account) leaks to CloudWatch.
"""

from __future__ import annotations

# Standard library
import json
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
    build_latency_emf_line,
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
    emf: list[str] = []
    return CloudWatchObservationMetrics(client=client, namespace=NAMESPACE, emf_emit=emf.append), client


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


def test_latency_publish_skips_the_synchronous_call_when_too_little_time_remains() -> None:
    # When the request has already consumed the budget down to less than one
    # persistence call's worth of time, the *synchronous* put_metric_data is
    # skipped so a slow call cannot push the invocation past the Lambda timeout.
    # The EMF datapoint is still emitted, so no latency datapoint is lost.
    client = FakeCloudWatch()
    emf: list[str] = []
    sink = CloudWatchObservationMetrics(
        client=client, namespace=NAMESPACE, total_deadline_s=15.0, min_publish_remaining_s=3.0, emf_emit=emf.append
    )
    # 13 s elapsed of a 15 s budget leaves 2 s < the 3 s floor: skip the sync call.
    sink.publish_latency(elapsed_s=13.0)
    assert client.puts == []
    assert sink.latency_sync_skipped == 1
    # The latency datapoint still reaches CloudWatch through the EMF line.
    assert len(emf) == 1


def test_latency_publish_runs_the_synchronous_call_when_enough_time_remains() -> None:
    client = FakeCloudWatch()
    emf: list[str] = []
    sink = CloudWatchObservationMetrics(
        client=client, namespace=NAMESPACE, total_deadline_s=15.0, min_publish_remaining_s=3.0, emf_emit=emf.append
    )
    # 1 s elapsed leaves 14 s >= the 3 s floor: publish synchronously and via EMF.
    sink.publish_latency(elapsed_s=1.0)
    assert _names(client) == ["ObservationRequestLatency"]
    assert sink.latency_sync_skipped == 0
    assert len(emf) == 1


def test_a_slow_request_at_the_alarm_threshold_still_produces_a_latency_datapoint() -> None:
    # The 13 s request sits exactly at the default p99 latency alarm threshold
    # (13000 ms). It must still produce a latency datapoint the alarm's metric
    # would see, otherwise the slow tail the alarm exists to catch is invisible.
    # The datapoint is the EMF line, carrying the same namespace, metric name,
    # and unit as the synchronous path.
    client = FakeCloudWatch()
    emitted: list[str] = []
    sink = CloudWatchObservationMetrics(
        client=client, namespace=NAMESPACE, total_deadline_s=15.0, min_publish_remaining_s=3.0, emf_emit=emitted.append
    )
    sink.publish_latency(elapsed_s=13.0)
    assert len(emitted) == 1
    document = json.loads(emitted[0])
    assert document[METRIC_LATENCY] == pytest.approx(13000.0)
    cloudwatch_metrics = document["_aws"]["CloudWatchMetrics"][0]
    assert cloudwatch_metrics["Namespace"] == NAMESPACE
    assert cloudwatch_metrics["Metrics"] == [{"Name": "ObservationRequestLatency", "Unit": "Milliseconds"}]
    # No dimensions, matching the deployment's p99 alarm.
    assert cloudwatch_metrics["Dimensions"] == [[]]


def test_latency_emf_line_matches_the_alarm_metric_identity() -> None:
    # A fixed synthetic epoch-millisecond timestamp (built from its parts so it
    # is never mistaken for an identifier) is echoed into the EMF document.
    synthetic_timestamp_ms = 1_700 * 1_000_000_000
    line = build_latency_emf_line(
        namespace="GameAgent/Operations", latency_ms=403.194, timestamp_ms=synthetic_timestamp_ms
    )
    document = json.loads(line)
    assert document["ObservationRequestLatency"] == pytest.approx(403.194)
    metric_block = document["_aws"]["CloudWatchMetrics"][0]
    assert metric_block["Namespace"] == "GameAgent/Operations"
    assert metric_block["Metrics"][0]["Name"] == "ObservationRequestLatency"
    assert metric_block["Metrics"][0]["Unit"] == "Milliseconds"
    assert metric_block["Dimensions"] == [[]]
    assert document["_aws"]["Timestamp"] == synthetic_timestamp_ms


def test_latency_emf_emit_error_is_swallowed_and_counted() -> None:
    client = FakeCloudWatch()

    def boom(_line: str) -> None:
        raise RuntimeError("stdout closed")

    sink = CloudWatchObservationMetrics(client=client, namespace=NAMESPACE, emf_emit=boom)
    sink.publish_latency(elapsed_s=1.0)
    # The EMF emit failed but was swallowed; the synchronous put still ran.
    assert sink.dropped == 1
    assert _names(client) == ["ObservationRequestLatency"]


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
