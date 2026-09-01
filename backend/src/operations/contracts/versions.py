"""Immutable v1 contract versions and operation lifecycle rules."""

from __future__ import annotations

# Standard library
from collections.abc import Mapping

CONTRACT_VERSION = "1.0"

SCHEMA_NAMES = frozenset(
    {
        "application-error",
        "approval-record",
        "authorization-decision",
        "common",
        "ledger-event",
        "operation-state-change",
        "playbook",
        "prepare-operation-request",
        "prepared-operation",
        "source-control-prepared-operation",
    }
)

OPERATION_STATES = frozenset(
    {
        "prepared",
        "pending_approval",
        "approved",
        "dispatched",
        "executing",
        "retry_pending",
        "succeeded",
        "failed",
        "rejected",
        "cancelled",
        "expired",
    }
)

STATE_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"prepared"}),
    "prepared": frozenset({"pending_approval", "rejected", "cancelled", "expired"}),
    "pending_approval": frozenset({"approved", "rejected", "cancelled", "expired"}),
    "approved": frozenset({"dispatched", "cancelled", "expired"}),
    "dispatched": frozenset({"executing", "retry_pending", "failed"}),
    "executing": frozenset({"succeeded", "retry_pending", "failed"}),
    "retry_pending": frozenset({"dispatched", "failed", "expired"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "rejected": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}

STATE_TRANSITION_REASONS: Mapping[tuple[str | None, str], str] = {
    (None, "prepared"): "OPERATION_PREPARED",
    ("prepared", "pending_approval"): "APPROVAL_REQUESTED",
    ("prepared", "rejected"): "APPROVAL_DENIED",
    ("prepared", "cancelled"): "CANCELLED",
    ("prepared", "expired"): "EXPIRED",
    ("pending_approval", "approved"): "APPROVAL_GRANTED",
    ("pending_approval", "rejected"): "APPROVAL_DENIED",
    ("pending_approval", "cancelled"): "CANCELLED",
    ("pending_approval", "expired"): "EXPIRED",
    ("approved", "dispatched"): "DISPATCHED",
    ("approved", "cancelled"): "CANCELLED",
    ("approved", "expired"): "EXPIRED",
    ("dispatched", "executing"): "EXECUTION_STARTED",
    ("dispatched", "retry_pending"): "RETRY_SCHEDULED",
    ("dispatched", "failed"): "EXECUTION_FAILED",
    ("executing", "succeeded"): "EXECUTION_SUCCEEDED",
    ("executing", "retry_pending"): "RETRY_SCHEDULED",
    ("executing", "failed"): "EXECUTION_FAILED",
    ("retry_pending", "dispatched"): "DISPATCHED",
    ("retry_pending", "failed"): "EXECUTION_FAILED",
    ("retry_pending", "expired"): "EXPIRED",
}


def is_supported_contract_version(version: str) -> bool:
    """Return whether this package gives the supplied version exact meaning."""
    return version == CONTRACT_VERSION


def validate_state_transition(previous_state: str | None, new_state: str, reason_code: str | None = None) -> None:
    """Reject lifecycle transitions not published by contract version 1.0."""
    if previous_state not in STATE_TRANSITIONS:
        raise ValueError(f"unknown previous operation state: {previous_state}")
    if new_state not in OPERATION_STATES:
        raise ValueError(f"unknown new operation state: {new_state}")
    if new_state not in STATE_TRANSITIONS[previous_state]:
        previous = previous_state or "<none>"
        raise ValueError(f"operation state transition is not allowed: {previous} -> {new_state}")
    expected_reason = STATE_TRANSITION_REASONS[(previous_state, new_state)]
    if reason_code is not None and reason_code != expected_reason:
        raise ValueError(f"reason_code must be {expected_reason} for transition")
