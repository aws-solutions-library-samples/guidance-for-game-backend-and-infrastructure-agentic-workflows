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

# A caller's own mistyped or unsupported fleet is a client error, not a service
# fault, so it is not counted in ``ObservationFailures`` and never feeds the
# failures alarm. The deadline reason is reported as a timeout instead.
_CLIENT_CAUSED_FAILURE_REASONS = frozenset({"not_found", "contract_invalid"})


class CloudWatchClient(Protocol):
    """The narrow slice of the boto3 CloudWatch client this sink uses."""

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]: ...


class CloudWatchObservationMetrics:
    """Translate bounded observe events into the four named CloudWatch metrics."""

    def __init__(
        self,
        *,
        client: CloudWatchClient,
        namespace: str,
        total_deadline_s: float | None = None,
        min_publish_remaining_s: float | None = None,
    ) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._client = client
        self._namespace = namespace
        self._dropped = 0
        # The optional latency-publish floor: when fewer than
        # ``min_publish_remaining_s`` seconds remain of ``total_deadline_s``, the
        # end-to-end latency publish is skipped so a slow put_metric_data cannot
        # push the invocation past the Lambda timeout.
        self._total_deadline_s = total_deadline_s
        self._min_publish_remaining_s = min_publish_remaining_s
        self._latency_skipped = 0

    def record(self, name: str, value: float, *, dimensions: Mapping[str, str] | None = None) -> None:
        reason = dimensions.get("reason") if isinstance(dimensions, Mapping) else None
        if name == "observation.failed":
            # A provider deadline overrun surfaces as a timeout; a client-caused
            # reason (an unknown or unsupported fleet) is the caller's error and
            # is not counted as a service failure; every other reason is a
            # generic ObservationFailures increment.
            if reason == "deadline":
                self._safe_put(METRIC_TIMEOUTS, 1.0)
            elif reason in _CLIENT_CAUSED_FAILURE_REASONS:
                return
            else:
                self._safe_put(METRIC_FAILURES, 1.0)
            return
        if name == "observation.timeout":
            self._safe_put(METRIC_TIMEOUTS, 1.0)
            return
        if name == "observation.in_progress":
            self._safe_put(METRIC_STUCK, 1.0)
            return
        # observation.recorded / observation.replay / observation.reclaimed /
        # observation.denied carry no dedicated CloudWatch metric here; latency
        # is published separately.

    def put_latency_ms(self, latency_ms: float) -> None:
        """Publish end-to-end request latency in milliseconds."""
        self._safe_put(METRIC_LATENCY, float(latency_ms), unit="Milliseconds")

    def publish_latency(self, *, elapsed_s: float) -> None:
        """Publish latency unless too little of the request budget remains.

        When a budget floor is configured and fewer than
        ``min_publish_remaining_s`` seconds remain of ``total_deadline_s``, the
        publish is skipped (and counted) so a slow ``put_metric_data`` can never
        push the invocation past the Lambda timeout. Without a configured floor
        the latency is always published.
        """
        if self._total_deadline_s is not None and self._min_publish_remaining_s is not None:
            remaining = self._total_deadline_s - elapsed_s
            if remaining < self._min_publish_remaining_s:
                self._latency_skipped += 1
                return
        self.put_latency_ms(elapsed_s * 1000.0)

    @property
    def latency_skipped(self) -> int:
        """Count of latency publishes skipped because too little time remained."""
        return self._latency_skipped

    def _safe_put(self, metric_name: str, value: float, *, unit: str = "Count") -> None:
        """Publish one metric, swallowing any CloudWatch error.

        Metrics are observability, never part of the request's success contract:
        a ``put_metric_data`` failure must never replace a typed boundary error
        with a 500 or leave a snapshot stranded in ``observing``. A failure is
        swallowed and counted locally so the request's own outcome stands.
        """
        try:
            self._put(metric_name, value, unit=unit)
        except Exception:  # noqa: BLE001 - metrics must never break a request
            self._dropped += 1

    @property
    def dropped(self) -> int:
        """Count of metric publishes dropped because CloudWatch raised."""
        return self._dropped

    def _put(self, metric_name: str, value: float, *, unit: str = "Count") -> None:
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": unit}],
        )
