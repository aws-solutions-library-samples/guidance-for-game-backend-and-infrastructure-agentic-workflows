"""Tests for the narrow execution selection path (issue #439, track B).

The existing E3 executor accepts ``operation_id`` only and reloads the prepared
operation, approval, and state itself. The selection path is the single, narrow
seam that decides — from the *reloaded, server-owned* documents — whether a
reloaded operation is the v1 human-approved capacity operation (verified by the
untouched :class:`ExecutionVerifier` and its granted-approval branch) or the v2
bounded-autonomy operation (verified by :class:`AutonomyExecutionVerifier`).

The selection is made from immutable, server-owned fields (contract version,
phase, profile, capability version, authority decision enum) — never from model
output, request-body fields, or a smuggled approval. A v1 operation is never
routed through the autonomous verifier, and a v2 operation is never routed
through the human-approval branch.
"""

from __future__ import annotations

# Standard library
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_execution_selection import (
    ExecutionPath,
    SelectionError,
    select_execution_path,
)
from operations.contracts import load_json

V1_FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
V2_FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"


def _v1_operation() -> dict[str, Any]:
    return load_json(V1_FIXTURES / "gamelift-capacity-prepared-operation.valid.json")


def _v2_operation() -> dict[str, Any]:
    return load_json(V2_FIXTURES / "gamelift-capacity-autonomous-operation.valid.json")


def test_v1_prepared_operation_selects_human_approval_path() -> None:
    assert select_execution_path(_v1_operation()) is ExecutionPath.HUMAN_APPROVAL


def test_v2_autonomous_operation_selects_autonomous_path() -> None:
    assert select_execution_path(_v2_operation()) is ExecutionPath.AUTONOMOUS


def test_selection_uses_server_owned_contract_version_not_smuggled_field() -> None:
    """A v1 operation carrying a smuggled ``phase: operate`` or fake authority
    field is still routed by its immutable v1 contract version, not by the
    injected field."""
    operation = _v1_operation()
    operation["autonomy_policy"] = {"policy_id": "policy.attacker"}
    operation["human_approval_bypass"] = True
    assert select_execution_path(operation) is ExecutionPath.HUMAN_APPROVAL


def test_v2_operation_with_smuggled_v1_marker_still_autonomous() -> None:
    operation = _v2_operation()
    operation["future_executor_binding"] = {"executor_id": "executor.gamelift-capacity"}
    assert select_execution_path(operation) is ExecutionPath.AUTONOMOUS


def test_unknown_contract_version_fails_closed() -> None:
    operation = _v1_operation()
    operation["operation_contract_version"] = "9.9"
    with pytest.raises(SelectionError):
        select_execution_path(operation)


def test_non_mapping_fails_closed() -> None:
    with pytest.raises(SelectionError):
        select_execution_path("op_not_a_mapping")  # type: ignore[arg-type]


def test_missing_phase_or_profile_fails_closed() -> None:
    operation = _v2_operation()
    del operation["profile"]
    with pytest.raises(SelectionError):
        select_execution_path(operation)


def test_v2_phase_must_be_operate() -> None:
    operation = _v2_operation()
    operation["phase"] = "advise"
    with pytest.raises(SelectionError):
        select_execution_path(operation)
