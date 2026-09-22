"""CloudWatch metrics sink for the E4 control plane (issue #416).

Mirrors the E3 executor metrics sink: the control plane emits internal, bounded,
public-safe metric events; this sink translates them into named CloudWatch
metrics under the supplied namespace. Dimensions are always dropped — only the
bounded metric name and value cross to CloudWatch — so an account id, ARN,
workspace id, or config value can never leak through a metric.

* ``ControlApplied`` — an admin control change was applied (config advanced).
* ``ControlVersionConflict`` — a compare-and-set conflict (stale/racing write).
* ``ControlDenied`` — a control change was denied on authority.
* ``ControlPublicationReconciled`` — a committed-but-unpublished decision was
  re-published and confirmed on a lost-response retry (the reconciliation path).
* ``KillSwitchUnavailable`` — the kill-switch could not be read as a fresh, valid
  document (extension unavailable/malformed/stale); a phase failed closed.
* ``OperationsExpirySweepExpired`` — the number of operations expired in one
  periodic sweep run.
"""

from __future__ import annotations

# Standard library
from collections.abc import Mapping
from typing import Any, Protocol

METRIC_CONTROL_APPLIED = "ControlApplied"
METRIC_CONTROL_VERSION_CONFLICT = "ControlVersionConflict"
METRIC_CONTROL_DENIED = "ControlDenied"
METRIC_CONTROL_PUBLICATION_RECONCILED = "ControlPublicationReconciled"
METRIC_KILL_SWITCH_UNAVAILABLE = "KillSwitchUnavailable"
METRIC_EXPIRY_SWEEP_EXPIRED = "OperationsExpirySweepExpired"

_EVENT_METRICS = {
    "control.applied": METRIC_CONTROL_APPLIED,
    "control.version_conflict": METRIC_CONTROL_VERSION_CONFLICT,
    "control.denied": METRIC_CONTROL_DENIED,
    "control.publication_reconciled": METRIC_CONTROL_PUBLICATION_RECONCILED,
    "kill_switch.unavailable": METRIC_KILL_SWITCH_UNAVAILABLE,
}


class CloudWatchClient(Protocol):
    """The narrow slice of the boto3 CloudWatch client this sink uses."""

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]: ...


class CloudWatchControlMetrics:
    """Translate bounded control-plane events into named CloudWatch metrics."""

    def __init__(self, *, client: CloudWatchClient, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._client = client
        self._namespace = namespace

    def record(self, name: str, *, dimensions: Mapping[str, str] | None = None) -> None:
        """Publish the named metric for a bounded event; dimensions are ignored."""
        metric = _EVENT_METRICS.get(name)
        if metric is not None:
            self._put(metric, 1.0)

    def put_expiry_sweep_expired(self, count: int) -> None:
        """Publish the number of operations expired in one periodic sweep run."""
        self._put(METRIC_EXPIRY_SWEEP_EXPIRED, float(count))

    def _put(self, metric_name: str, value: float, *, unit: str = "Count") -> None:
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": unit}],
        )
