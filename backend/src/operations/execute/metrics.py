"""CloudWatch metrics sink for the E3 execute Lambdas (issue #415).

The executor service emits internal, bounded, public-safe metric events; this
sink translates them into the named CloudWatch metrics the deployment monitors:

* ``ExecutionFailures`` — an execution that terminated FAILED for any reason
  (provider error, state drift, verification failure).
* ``ExecutionReconciled`` — an execution that found the fleet already at target
  and recorded a reconciled success with no provider write.
* ``ExecutionHumanReconciliationRequired`` — an execution whose write result was
  inconclusive and could not be conclusively confirmed; a human must reconcile.
* ``ExecutionProviderWrites`` — a single UpdateFleetCapacity write was issued
  (one per logical update at most).
* ``ExecutionRequestLatency`` — end-to-end request latency in milliseconds.

All metrics publish under the namespace supplied at construction. Dimensions are
dropped: only the bounded, public-safe metric name and value cross to CloudWatch
— never an account id, ARN, fleet id, workspace id, or capacity value.
"""

from __future__ import annotations

# Standard library
from collections.abc import Mapping
from typing import Any, Protocol

METRIC_EXECUTION_FAILURES = "ExecutionFailures"
METRIC_RECONCILED = "ExecutionReconciled"
METRIC_HUMAN_RECONCILIATION = "ExecutionHumanReconciliationRequired"
METRIC_PROVIDER_WRITES = "ExecutionProviderWrites"
METRIC_LATENCY = "ExecutionRequestLatency"

_EVENT_METRICS = {
    "execution.failed": METRIC_EXECUTION_FAILURES,
    "execution.reconciled": METRIC_RECONCILED,
    "execution.human_reconciliation_required": METRIC_HUMAN_RECONCILIATION,
    "execution.provider_write_issued": METRIC_PROVIDER_WRITES,
}


class CloudWatchClient(Protocol):
    """The narrow slice of the boto3 CloudWatch client this sink uses."""

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]: ...


class CloudWatchExecutionMetrics:
    """Translate bounded execution events into named CloudWatch metrics."""

    def __init__(self, *, client: CloudWatchClient, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._client = client
        self._namespace = namespace

    def record(self, name: str, *, dimensions: Mapping[str, str] | None = None) -> None:
        """Publish the named CloudWatch metric for a bounded execution event.

        ``dimensions`` are intentionally ignored: no dimension ever crosses to
        CloudWatch, so a fleet id / workspace id can never leak through a metric.
        """
        metric = _EVENT_METRICS.get(name)
        if metric is not None:
            self._put(metric, 1.0)

    def put_latency_ms(self, latency_ms: float) -> None:
        """Publish end-to-end request latency in milliseconds."""
        self._put(METRIC_LATENCY, float(latency_ms), unit="Milliseconds")

    def _put(self, metric_name: str, value: float, *, unit: str = "Count") -> None:
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": unit}],
        )
