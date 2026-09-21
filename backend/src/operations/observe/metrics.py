"""CloudWatch metrics sink for the observe Lambda (issue #413).

The observe service emits internal, bounded metric events through the
:class:`~operations.observation.ObservationMetrics` port
(``observation.failed``, ``observation.timeout``, ``observation.recorded``,
``observation.replay``, ``observation.reclaimed``, ``observation.in_progress``,
``observation.denied``). This sink translates those public-safe events into the
four CloudWatch metrics the deployment monitors, named **exactly**:

* ``ObservationFailures`` — a request that failed for any non-timeout reason
  (provider error, contract, idempotency conflict, state conflict, hash).
* ``ObservationTimeouts`` — a request that failed because a provider read or the
  persistence step exceeded its wall-clock deadline.
* ``StuckOperations`` — an operation observed to be already in progress under a
  live lease when a retry arrived (a potential single-flight stall).
* ``ObservationRequestLatency`` — end-to-end request latency in milliseconds.

All metrics publish under the namespace supplied at construction (from
``GBAW_OPERATIONS_METRIC_NAMESPACE``). Dimensions are dropped: only the bounded,
public-safe metric name and value cross to CloudWatch — never an account id,
ARN, fleet id, or workspace id.
"""

from __future__ import annotations

# Standard library
from collections.abc import Mapping
from typing import Any, Protocol

METRIC_FAILURES = "ObservationFailures"
METRIC_TIMEOUTS = "ObservationTimeouts"
METRIC_STUCK = "StuckOperations"
METRIC_LATENCY = "ObservationRequestLatency"


class CloudWatchClient(Protocol):
    """The narrow slice of the boto3 CloudWatch client this sink uses."""

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]: ...


class CloudWatchObservationMetrics:
    """Translate bounded observe events into the four named CloudWatch metrics."""

    def __init__(self, *, client: CloudWatchClient, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._client = client
        self._namespace = namespace

    def record(self, name: str, value: float, *, dimensions: Mapping[str, str] | None = None) -> None:
        reason = dimensions.get("reason") if isinstance(dimensions, Mapping) else None
        if name == "observation.failed":
            # A provider deadline overrun surfaces as a timeout; every other
            # failure reason is a generic failure. A provider *error* (non-
            # timeout) is reported as a failure; only the deadline reason and an
            # explicit timeout event count as timeouts.
            if reason == "deadline":
                self._put(METRIC_TIMEOUTS, 1.0)
            else:
                self._put(METRIC_FAILURES, 1.0)
            return
        if name == "observation.timeout":
            self._put(METRIC_TIMEOUTS, 1.0)
            return
        if name == "observation.in_progress":
            self._put(METRIC_STUCK, 1.0)
            return
        # observation.recorded / observation.replay / observation.reclaimed /
        # observation.denied carry no dedicated CloudWatch metric here; latency
        # is published separately.

    def put_latency_ms(self, latency_ms: float) -> None:
        """Publish end-to-end request latency in milliseconds."""
        self._put(METRIC_LATENCY, float(latency_ms), unit="Milliseconds")

    def _put(self, metric_name: str, value: float, *, unit: str = "Count") -> None:
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": unit}],
        )
