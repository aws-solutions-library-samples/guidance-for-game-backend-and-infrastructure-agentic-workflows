#!/usr/bin/env python3
"""Unit test: the Step Functions task payload to the executor contains only ``operation_id``.

The Durable_Workflow (Step Functions Standard) invokes the isolated executor with **only** the
opaque ``operation_id`` — no files, target, revision, or free-form instruction ever reach the
executor through the workflow (Req 3.3, echoing 4.2/4.3). This asserts that guarantee directly
against the authored ASL definition at
``infrastructure/statemachine/scm-executor-workflow.asl.json``: the single ``lambda:invoke``
task's ``Parameters.Payload`` maps exactly one key, ``operation_id.$``, to ``$.operation_id``.

Validates: Requirements 3.3
"""

# Standard library
import json
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

# tests/unit/<this file> -> tests/unit -> tests -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ASL_PATH = _REPO_ROOT / "infrastructure" / "statemachine" / "scm-executor-workflow.asl.json"

_LAMBDA_INVOKE_RESOURCE = "arn:aws:states:::lambda:invoke"


@pytest.fixture(scope="module")
def state_machine() -> dict:
    """Parse the authored ASL definition."""
    assert _ASL_PATH.is_file(), f"ASL definition not found at {_ASL_PATH}"
    parsed = json.loads(_ASL_PATH.read_text())
    assert isinstance(parsed, dict), "ASL root is not an object"
    return parsed


def _lambda_invoke_states(state_machine: dict) -> list[dict]:
    """Return every state that invokes a Lambda via the ``lambda:invoke`` service integration."""
    states = state_machine.get("States", {})
    return [
        body for body in states.values() if isinstance(body, dict) and body.get("Resource") == _LAMBDA_INVOKE_RESOURCE
    ]


def test_executor_is_invoked_by_exactly_one_lambda_task(state_machine: dict) -> None:
    """The workflow drives the executor through a single ``lambda:invoke`` task."""
    invoke_states = _lambda_invoke_states(state_machine)
    assert len(invoke_states) == 1, f"expected exactly one lambda:invoke task, found {len(invoke_states)}"


def test_executor_task_payload_contains_only_operation_id(state_machine: dict) -> None:
    """(Req 3.3) The executor task payload passes ONLY the opaque operation_id and nothing else."""
    invoke_state = _lambda_invoke_states(state_machine)[0]
    payload = invoke_state.get("Parameters", {}).get("Payload")
    assert isinstance(payload, dict), "executor invoke task has no Payload object"

    # Exactly one key, and it is the operation_id path binding — no files/target/revision/etc.
    assert set(payload.keys()) == {"operation_id.$"}, f"payload carries more than operation_id: {sorted(payload)}"
    assert payload["operation_id.$"] == "$.operation_id"
