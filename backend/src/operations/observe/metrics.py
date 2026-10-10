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

End-to-end request latency is published as a CloudWatch embedded-metric-format
(EMF) log line on **every** request, under the same namespace and metric name
(``ObservationRequestLatency``, unit ``Milliseconds``, no dimensions) as the
synchronous ``put_metric_data`` path, so the deployment's p99 latency alarm
sees a datapoint for a request of any duration — including a slow request near
the Lambda timeout, which the alarm exists to catch. The EMF line requires no
synchronous call, so it never spends request budget. When a latency floor is
configured, the synchronous ``put_metric_data`` is additionally attempted only
while enough of the request budget remains; the EMF datapoint stands either
way, and a skipped synchronous publish is logged.
"""

from __future__ import annotations

# Standard library
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

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


def _default_emf_emit(line: str) -> None:
    """Write one EMF log line to stdout for CloudWatch Logs extraction.

    The Lambda runtime forwards stdout to CloudWatch Logs, where the embedded
    metric format is parsed into a CloudWatch metric. A plain ``print`` keeps
    the line a single, self-contained JSON document on its own log event.
    """
    print(line, flush=True)


def build_latency_emf_line(*, namespace: str, latency_ms: float, timestamp_ms: int | None = None) -> str:
    """Build a CloudWatch EMF log line for the end-to-end latency metric.

    The metric is emitted with the exact namespace, metric name
    (``ObservationRequestLatency``), unit (``Milliseconds``), and empty
    dimension set the synchronous ``put_metric_data`` path uses, so the
    deployment's p99 latency alarm — which specifies this namespace and metric
    with no dimensions — aggregates EMF datapoints and synchronous datapoints
    identically.
    """
    when = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    document = {
        "_aws": {
            "Timestamp": when,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    # An empty dimension list publishes the metric at the
                    # namespace root (no dimensions), matching the alarm.
                    "Dimensions": [[]],
                    "Metrics": [{"Name": METRIC_LATENCY, "Unit": "Milliseconds"}],
                }
            ],
        },
        METRIC_LATENCY: float(latency_ms),
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


class CloudWatchObservationMetrics:
    """Translate bounded observe events into the four named CloudWatch metrics."""

    def __init__(
        self,
        *,
        client: CloudWatchClient,
        namespace: str,
        total_deadline_s: float | None = None,
        min_publish_remaining_s: float | None = None,
        emf_emit: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._client = client
        self._namespace = namespace
        self._dropped = 0
        # The optional latency-publish floor: when fewer than
        # ``min_publish_remaining_s`` seconds remain of ``total_deadline_s``, the
        # *synchronous* latency publish is skipped so a slow put_metric_data
        # cannot push the invocation past the Lambda timeout. The EMF latency
        # line is emitted regardless, so the latency datapoint is never dropped.
        self._total_deadline_s = total_deadline_s
        self._min_publish_remaining_s = min_publish_remaining_s
        self._emf_emit = emf_emit or _default_emf_emit
        self._latency_sync_skipped = 0

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
        """Publish end-to-end request latency in milliseconds (synchronous)."""
        self._safe_put(METRIC_LATENCY, float(latency_ms), unit="Milliseconds")

    def emit_latency_emf(self, latency_ms: float) -> None:
        """Emit the end-to-end latency as an EMF log line (no synchronous call).

        This is the authoritative latency datapoint for the p99 alarm: it is
        emitted for every request regardless of remaining budget, and it costs
        no CloudWatch API call, so it can never push the invocation past the
        Lambda timeout. Any emit error is swallowed and counted so publishing
        latency never breaks a request.
        """
        try:
            self._emf_emit(build_latency_emf_line(namespace=self._namespace, latency_ms=float(latency_ms)))
        except Exception:  # noqa: BLE001 - metrics must never break a request
            self._dropped += 1

    def publish_latency(self, *, elapsed_s: float) -> None:
        """Publish end-to-end latency, keeping a datapoint for every request.

        The latency is always emitted as an EMF log line so the p99 alarm sees a
        datapoint for a request of any duration — including a slow request near
        the Lambda timeout. When a budget floor is configured and fewer than
        ``min_publish_remaining_s`` seconds remain of ``total_deadline_s``, the
        additional *synchronous* ``put_metric_data`` is skipped (and the skip is
        logged) so a slow call can never push the invocation past the timeout;
        the EMF datapoint already stands. Without a configured floor the
        synchronous publish also runs.
        """
        latency_ms = elapsed_s * 1000.0
        # Always emit the EMF datapoint first: it is the datapoint the alarm
        # relies on and it makes no synchronous call.
        self.emit_latency_emf(latency_ms)
        if self._total_deadline_s is not None and self._min_publish_remaining_s is not None:
            remaining = self._total_deadline_s - elapsed_s
            if remaining < self._min_publish_remaining_s:
                self._latency_sync_skipped += 1
                logger.info(
                    "observe.latency.sync_publish_skipped",
                    extra={"remaining_s": round(remaining, 3), "floor_s": self._min_publish_remaining_s},
                )
                return
        self.put_latency_ms(latency_ms)

    @property
    def latency_sync_skipped(self) -> int:
        """Count of synchronous latency publishes skipped by the budget floor.

        The EMF latency datapoint is emitted for every request regardless, so a
        non-zero value here means only that the redundant synchronous call was
        skipped, not that a latency datapoint was lost.
        """
        return self._latency_sync_skipped

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
