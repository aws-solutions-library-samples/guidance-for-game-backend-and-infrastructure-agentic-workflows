#!/usr/bin/env python3
"""Integration test: Standard workflow, Streams enabled, approval-transition dispatch (task 9.9).

The Durable_Workflow is AWS Step Functions **Standard**, the Prepared_Operation_Store has
DynamoDB Streams enabled, and the approval-transition dispatch triggers a workflow execution
(Req 3.1, 3.2, 3.4). These are deployment-topology guarantees not amenable to property-based
testing (design → Testing Strategy → "Template / synth and integration tests"), so they are
asserted against the authored ASL definition
(``infrastructure/statemachine/scm-executor-workflow.asl.json``) and the gated CloudFormation
template (``infrastructure/cloudformation/06-scm-executor.yaml``) exactly as the existing synth
tests do (mirroring ``test_executor_cfn_isolation_smoke.py`` / ``test_executor_workflow_payload_unit.py``).

This test is intentionally marked ``integration`` and is therefore EXCLUDED from the default
``not integration`` runner; it is run explicitly.

Validates: Requirements 3.1, 3.2, 3.4
"""

# Standard library
import json
from pathlib import Path

# Third-party packages
import pytest
import yaml

pytestmark = pytest.mark.integration

# tests/unit/<this file> -> tests/unit -> tests -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_PATH = _REPO_ROOT / "infrastructure" / "cloudformation" / "06-scm-executor.yaml"
_ASL_PATH = _REPO_ROOT / "infrastructure" / "statemachine" / "scm-executor-workflow.asl.json"

_WRITE_CONDITION = "ScmWriteEnabled"
_STORE_TABLE = "ScmPreparedOperationTable"
_WORKFLOW = "ScmWorkflow"
_DISPATCH_PIPE = "ScmApprovalDispatchPipe"
_DISPATCH_ROLE = "ScmDispatchPipeRole"
_LAMBDA_INVOKE_RESOURCE = "arn:aws:states:::lambda:invoke"


# --- CloudFormation-aware YAML loader (mirrors test_executor_cfn_isolation_smoke.py) -------


class _CfnLoader(yaml.SafeLoader):
    """A ``SafeLoader`` subclass that understands CloudFormation short-form tags."""


def _cfn_multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    """Represent any ``!Tag`` intrinsic as a plain mapping so parsing succeeds."""
    if tag_suffix in ("Ref", "Condition"):
        key = tag_suffix
    else:
        key = f"Fn::{tag_suffix}"

    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = None
    return {key: value}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


# --- Fixtures ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> dict:
    """Parse the gated executor CloudFormation template with the CFN-aware loader."""
    assert _TEMPLATE_PATH.is_file(), f"template not found at {_TEMPLATE_PATH}"
    parsed = yaml.load(_TEMPLATE_PATH.read_text(), Loader=_CfnLoader)  # noqa: S506 - safe subclass
    assert isinstance(parsed, dict), "template root is not a mapping"
    return parsed


@pytest.fixture(scope="module")
def state_machine() -> dict:
    """Parse the authored ASL definition."""
    assert _ASL_PATH.is_file(), f"ASL definition not found at {_ASL_PATH}"
    parsed = json.loads(_ASL_PATH.read_text())
    assert isinstance(parsed, dict), "ASL root is not an object"
    return parsed


# --- Helpers -------------------------------------------------------------------------------


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _resource(template: dict, logical_id: str) -> dict:
    res = template.get("Resources", {}).get(logical_id)
    assert isinstance(res, dict), f"{logical_id} not found in Resources"
    return res


def _role_statements(role: dict) -> list:
    statements: list = []
    for policy in _as_list(role.get("Properties", {}).get("Policies")):
        if not isinstance(policy, dict):
            continue
        for statement in _as_list(policy.get("PolicyDocument", {}).get("Statement")):
            if isinstance(statement, dict):
                statements.append(statement)
    return statements


def _contains_ref(value: object, logical_id: str) -> bool:
    """Return True if a CFN value (Ref / GetAtt / nested) references ``logical_id``."""
    if isinstance(value, str):
        return logical_id in value
    if isinstance(value, dict):
        return any(_contains_ref(v, logical_id) for v in value.values())
    if isinstance(value, list):
        return any(_contains_ref(v, logical_id) for v in value)
    return False


# --- Req 3.1: the Durable_Workflow is Step Functions Standard ------------------------------


def test_workflow_is_step_functions_standard(template: dict) -> None:
    """(Req 3.1) The workflow is an AWS::StepFunctions::StateMachine of type STANDARD, gated."""
    workflow = _resource(template, _WORKFLOW)
    assert workflow.get("Type") == "AWS::StepFunctions::StateMachine"
    assert workflow.get("Condition") == _WRITE_CONDITION
    assert workflow.get("Properties", {}).get("StateMachineType") == "STANDARD"


def test_asl_definition_drives_executor_over_operation_id(state_machine: dict) -> None:
    """(Req 3.1, 3.3) The authored ASL is the durable workflow: it starts at LoadAndVerify and
    invokes the executor through a single lambda:invoke task carrying only operation_id."""
    assert state_machine.get("StartAt") == "LoadAndVerify"
    states = state_machine.get("States", {})
    invoke_states = [
        body for body in states.values() if isinstance(body, dict) and body.get("Resource") == _LAMBDA_INVOKE_RESOURCE
    ]
    assert len(invoke_states) == 1, "expected exactly one executor lambda:invoke task"
    payload = invoke_states[0].get("Parameters", {}).get("Payload")
    assert payload == {"operation_id.$": "$.operation_id"}


# --- Req 3.2 / 8.8: Streams are enabled on the Prepared_Operation_Store ---------------------


def test_streams_enabled_on_prepared_operation_store(template: dict) -> None:
    """(Req 3.2, 8.8) The store enables DynamoDB Streams with NEW_AND_OLD_IMAGES for dispatch."""
    table = _resource(template, _STORE_TABLE)
    assert table.get("Type") == "AWS::DynamoDB::Table"
    stream_spec = table.get("Properties", {}).get("StreamSpecification", {})
    assert stream_spec.get("StreamViewType") == "NEW_AND_OLD_IMAGES"


# --- Req 3.4: the approval-transition dispatch triggers a workflow execution ---------------


def test_dispatch_reads_store_stream_and_targets_the_workflow(template: dict) -> None:
    """(Req 3.4, 8.8) The dispatch pipe sources the store's stream and targets the workflow,
    filtered to the APPROVAL# transition INSERT, so an approval transition starts an execution."""
    pipe = _resource(template, _DISPATCH_PIPE)
    assert pipe.get("Type") == "AWS::Pipes::Pipe"
    assert pipe.get("Condition") == _WRITE_CONDITION

    props = pipe.get("Properties", {})
    # Source is the store's stream; target is the durable workflow.
    assert _contains_ref(props.get("Source"), _STORE_TABLE), "pipe source is not the store stream"
    assert _contains_ref(props.get("Target"), _WORKFLOW), "pipe target is not the workflow"

    # The filter restricts dispatch to the APPROVAL# transition (approval state change).
    filters = props.get("SourceParameters", {}).get("FilterCriteria", {}).get("Filters", [])
    filter_blob = json.dumps(filters)
    assert "APPROVAL#" in filter_blob, "dispatch is not filtered to the APPROVAL# transition"
    assert "INSERT" in filter_blob, "dispatch is not filtered to the transition INSERT"

    # Only the opaque operation_id is mapped into the workflow input.
    input_template = props.get("TargetParameters", {}).get("StepFunctionStateMachineParameters", {})
    # The InputTemplate lives alongside StepFunctionStateMachineParameters under TargetParameters.
    target_params = props.get("TargetParameters", {})
    assert "operation_id" in json.dumps(target_params), "dispatch does not map operation_id into the workflow input"
    assert input_template.get("InvocationType") == "FIRE_AND_FORGET"


def test_dispatch_role_can_start_workflow_execution(template: dict) -> None:
    """(Req 3.4) The dispatch pipe role is granted states:StartExecution on the workflow, so the
    approval-transition dispatch can actually trigger a workflow execution."""
    role = _resource(template, _DISPATCH_ROLE)
    start_execution = [
        s
        for s in _role_statements(role)
        if s.get("Effect") == "Allow" and "states:StartExecution" in _as_list(s.get("Action"))
    ]
    assert start_execution, "dispatch role has no states:StartExecution grant"
    targets = [r for s in start_execution for r in _as_list(s.get("Resource"))]
    assert any(_contains_ref(t, _WORKFLOW) for t in targets), "StartExecution grant is not scoped to the workflow"
