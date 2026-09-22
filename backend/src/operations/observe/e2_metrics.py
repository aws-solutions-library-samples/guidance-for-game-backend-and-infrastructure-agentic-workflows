"""CloudWatch metrics sink for the E2 approval surface (issue #414).

The E2 prepare/approval surface publishes exactly four bounded, public-safe
CloudWatch metrics, named **exactly** and with **no dimensions or identifiers**:

* ``PreparationFailures`` — a ``POST /operations/prepare`` failed (either a
  bounded conflict/deny or an unexpected failure).
* ``ApprovalFailures`` — an approve/reject failed for any non-expiry reason
  (authorization denied, contract invalid, hash mismatch, state conflict, ...).
* ``ApprovalExpired`` — an approve/reject/expiry failed because the operation or
  approval window had already elapsed.
* ``CancellationConflicts`` — a cancel (or any terminal decision) lost a fenced
  conditional race and reported a state conflict.

Only the bounded metric name and the value ``1.0`` cross to CloudWatch — never
an account id, ARN, fleet id, workspace id, operation id, or any other
identifier. The namespace is supplied at construction (from
``GBAW_OPERATIONS_METRIC_NAMESPACE``).
"""

from __future__ import annotations

# Standard library
from typing import Any, Protocol

METRIC_PREPARATION_FAILURES = "PreparationFailures"
METRIC_APPROVAL_FAILURES = "ApprovalFailures"
METRIC_APPROVAL_EXPIRED = "ApprovalExpired"
METRIC_CANCELLATION_CONFLICTS = "CancellationConflicts"

# Public-safe internal events -> CloudWatch metric name. Anything not in this map
# publishes nothing (fails silent, never fabricates a metric).
_EVENT_METRICS = {
    "preparation.failed": METRIC_PREPARATION_FAILURES,
    "approval.failed": METRIC_APPROVAL_FAILURES,
    "approval.expired": METRIC_APPROVAL_EXPIRED,
    "cancellation.conflict": METRIC_CANCELLATION_CONFLICTS,
}


class CloudWatchClient(Protocol):
    """The narrow slice of the boto3 CloudWatch client this sink uses."""

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]: ...


class CloudWatchApprovalMetrics:
    """Translate bounded E2 events into the four named CloudWatch metrics."""

    def __init__(self, *, client: CloudWatchClient, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._client = client
        self._namespace = namespace

    def record(self, event: str) -> None:
        """Publish the bounded metric for one E2 event, or nothing if unknown."""
        metric_name = _EVENT_METRICS.get(event)
        if metric_name is None:
            return
        # No Dimensions key is ever added: only the bounded name + value cross.
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[{"MetricName": metric_name, "Value": 1.0, "Unit": "Count"}],
        )
