"""Bounded, code-owned projections for raw GameLift provider responses.

Part of #457. The GameLift specialist reaches AWS GameLift through boto3
``describe_*`` calls whose raw responses are provider-controlled: they carry
full ARNs (which embed the account ID), may grow unbounded across locations and
policies, and their exceptions carry provider-authored text. None of that may
reach model context verbatim.

This module keeps the transform *in code we own* rather than trusting the model
to ignore sensitive fields:

* Each ``project_*`` function copies only an explicit allowlist of operational
  fields from each item. Unknown / future / customer-controlled fields
  (endpoints, credential ARNs, private IPs, arbitrary nested blobs) are dropped
  by construction, not stripped by pattern.
* Full ARNs are never projected. Caller-supplied identifiers (the ``fleet_id``
  argument and its echo) are non-sensitive and retained for correlation.
* Collections are bounded by ``GAMELIFT_MAX_PROJECTED_ITEMS``; exceeding the cap
  or a residual ``NextToken`` marks the result ``truncated`` so the model is
  told the view is partial instead of silently losing rows.
* Provider exceptions are mapped to a small, typed, sanitized error vocabulary.
  The raw exception message is logged server-side only.

Dispositions are kept mutually distinct via the ``status`` field:

* ``ok``        — full result set returned.
* ``empty``     — the call succeeded and returned zero items.
* ``denied``    — the provider refused the call (authorization / access).
* ``incomplete``— the call failed, or only part of the result set was retrieved
                  (residual ``NextToken`` with no client-side cap hit).
* ``truncated`` — the result set was bounded by this projection's item cap.
"""

from __future__ import annotations

# Standard library
from typing import Any, Callable

# Third-party packages
from botocore.exceptions import BotoCoreError, ClientError

# Local modules
from utils.logger import logger

# Hard cap on how many projected items may reach model context per call. A
# single fleet can report many location rows and many scaling policies; without
# a cap an adversarial or unusual account could flood the context window.
GAMELIFT_MAX_PROJECTED_ITEMS = 100

# Disposition vocabulary — kept distinct on purpose (see module docstring).
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DENIED = "denied"
STATUS_INCOMPLETE = "incomplete"
STATUS_TRUNCATED = "truncated"

# Typed, sanitized error codes. These are stable identifiers the model may see;
# they carry no provider-authored text, ARNs, or network coordinates.
ERROR_ACCESS_DENIED = "access_denied"
ERROR_NOT_FOUND = "not_found"
ERROR_THROTTLED = "throttled"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_PROVIDER_ERROR = "provider_error"

# AWS/GameLift error codes that mean "authorization refused".
_DENIED_CODES = {
    "AccessDeniedException",
    "AccessDenied",
    "UnauthorizedException",
    "UnauthorizedOperation",
    "NotAuthorized",
}
_NOT_FOUND_CODES = {"NotFoundException", "ResourceNotFoundException"}
_THROTTLE_CODES = {"ThrottlingException", "Throttling", "TooManyRequestsException", "LimitExceededException"}
_INVALID_CODES = {"InvalidRequestException", "ValidationException", "InvalidFleetStatusException"}


def classify_error(exc: BaseException) -> str:
    """Map a provider exception to a typed, sanitized error code.

    The raw message is never returned; callers log it server-side. Only the
    stable code crosses into model context.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc.response, dict) else ""
        if code in _DENIED_CODES:
            return ERROR_ACCESS_DENIED
        if code in _NOT_FOUND_CODES:
            return ERROR_NOT_FOUND
        if code in _THROTTLE_CODES:
            return ERROR_THROTTLED
        if code in _INVALID_CODES:
            return ERROR_INVALID_REQUEST
        return ERROR_PROVIDER_ERROR
    if isinstance(exc, BotoCoreError):
        return ERROR_PROVIDER_ERROR
    return ERROR_PROVIDER_ERROR


def _status_for_error_code(code: str) -> str:
    """Denied maps to the distinct ``denied`` disposition; all else is incomplete."""
    return STATUS_DENIED if code == ERROR_ACCESS_DENIED else STATUS_INCOMPLETE


def error_result(collection_key: str, exc: BaseException) -> dict[str, Any]:
    """Build a sanitized error result for a failed provider call."""
    code = classify_error(exc)
    return {
        "status": _status_for_error_code(code),
        collection_key: [],
        "error": {"code": code},
    }


def _copy_allowed(item: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """Return only allowlisted, non-null fields from ``item``.

    Anything not named in ``allowed`` — including future provider fields, full
    ARNs, endpoints, and arbitrary nested blobs — is dropped by construction.
    """
    projected: dict[str, Any] = {}
    for key in allowed:
        value = item.get(key)
        if value is not None:
            projected[key] = value
    return projected


def _project_collection(
    response: dict[str, Any],
    collection_key: str,
    project_item: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Bound, project, and classify a successful provider response.

    Empty, ok, truncated (cap hit), and incomplete (residual NextToken)
    dispositions are kept distinct.
    """
    raw_items = response.get(collection_key) or []
    projected = [project_item(item) for item in raw_items[:GAMELIFT_MAX_PROJECTED_ITEMS]]

    result: dict[str, Any] = {collection_key: projected}

    capped = len(raw_items) > GAMELIFT_MAX_PROJECTED_ITEMS
    has_more_pages = bool(response.get("NextToken"))

    if not projected:
        result["status"] = STATUS_EMPTY
        return result

    if capped:
        result["status"] = STATUS_TRUNCATED
        result["truncated"] = True
    elif has_more_pages:
        # More pages exist but we returned a single, bounded call: partial view.
        result["status"] = STATUS_INCOMPLETE
        result["truncated"] = True
    else:
        result["status"] = STATUS_OK
    return result


# ---------------------------------------------------------------------------
# Per-operation allowlists and item projectors
# ---------------------------------------------------------------------------
_UTILIZATION_FIELDS = (
    "FleetId",
    "ActiveServerProcessCount",
    "ActiveGameSessionCount",
    "CurrentPlayerSessionCount",
    "MaximumPlayerSessionCount",
    "Location",
)

_CAPACITY_FIELDS = (
    "FleetId",
    "InstanceType",
    "Location",
)
_INSTANCE_COUNT_FIELDS = ("DESIRED", "MINIMUM", "MAXIMUM", "PENDING", "ACTIVE", "IDLE", "TERMINATING")
_GROUP_COUNT_FIELDS = ("PENDING", "ACTIVE", "IDLE", "TERMINATING")

_SCALING_POLICY_FIELDS = (
    "FleetId",
    "Name",
    "Status",
    "ScalingAdjustment",
    "ScalingAdjustmentType",
    "ComparisonOperator",
    "Threshold",
    "EvaluationPeriods",
    "MetricName",
    "PolicyType",
    "UpdateStatus",
    "Location",
)


def _project_utilization_item(item: dict[str, Any]) -> dict[str, Any]:
    return _copy_allowed(item, _UTILIZATION_FIELDS)


def _project_capacity_item(item: dict[str, Any]) -> dict[str, Any]:
    projected = _copy_allowed(item, _CAPACITY_FIELDS)
    instance_counts = item.get("InstanceCounts")
    if isinstance(instance_counts, dict):
        counts = _copy_allowed(instance_counts, _INSTANCE_COUNT_FIELDS)
        if counts:
            projected["InstanceCounts"] = counts
    group_counts = item.get("GameServerContainerGroupCounts")
    if isinstance(group_counts, dict):
        counts = _copy_allowed(group_counts, _GROUP_COUNT_FIELDS)
        if counts:
            projected["GameServerContainerGroupCounts"] = counts
    return projected


def _project_scaling_policy_item(item: dict[str, Any]) -> dict[str, Any]:
    projected = _copy_allowed(item, _SCALING_POLICY_FIELDS)
    target = item.get("TargetConfiguration")
    if isinstance(target, dict):
        target_value = target.get("TargetValue")
        if target_value is not None:
            projected["TargetConfiguration"] = {"TargetValue": target_value}
    return projected


def project_fleet_utilization(response: dict[str, Any]) -> dict[str, Any]:
    return _project_collection(response, "FleetUtilization", _project_utilization_item)


def project_fleet_capacity(response: dict[str, Any]) -> dict[str, Any]:
    return _project_collection(response, "FleetCapacity", _project_capacity_item)


def project_scaling_policies(response: dict[str, Any]) -> dict[str, Any]:
    return _project_collection(response, "ScalingPolicies", _project_scaling_policy_item)


def log_sanitized_failure(operation: str, fleet_id: str, exc: BaseException) -> None:
    """Log the full provider error server-side only.

    Model-visible output receives a typed code via :func:`error_result`; the raw
    message (which may embed ARNs / account IDs / URLs) stays in logs.
    """
    logger.error(f"GameLift {operation} failed for fleet {fleet_id} [{classify_error(exc)}]: {exc}")
