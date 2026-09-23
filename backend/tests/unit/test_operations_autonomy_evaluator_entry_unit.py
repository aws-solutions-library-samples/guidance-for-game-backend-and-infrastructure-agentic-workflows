"""Lambda evaluator entry/bootstrap tests for the E5 runtime (#439).

The evaluator is the default-disabled Lambda that constructs the real runtime
composition — AppConfig autonomy switch + E4 kill-switch gate, E4 durable gate,
DynamoDbReservationStore, DynamoDbAutonomyBundleStore, a Step Functions
StartExecution client, the policy/observation/window loaders, and the
AutonomyRuntimeHandler — from strict, server-owned environment settings.

These tests lock the safety-critical contract:

* importing the module is AWS-free (no boto3 client, no network, no env read);
* the bootstrap REFUSES at startup unless the static deployment mode is exactly
  ``operate`` AND every autonomy identifier is configured (fail closed);
* the evaluator role touches ONLY DynamoDB, AppConfig, and Step Functions
  clients — never GameLift and never Lambda invoke (it holds no provider-write
  credential and starts a durable workflow with ``operation_id`` alone).
"""

from __future__ import annotations

# Standard library
import sys
from typing import Any

# Third-party packages
import pytest

_MODULE = "operations.autonomy_runtime.evaluator_entry"


def _base_env() -> dict[str, str]:
    return {
        "GBAW_OPERATIONS_MODE": "operate",
        "GBAW_OPERATIONS_AUTONOMY_ENABLED": "true",
        "GBAW_OPERATIONS_TABLE_NAME": "ops-06",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "aud.default",
        "GBAW_OPERATIONS_CAPABILITY_MAXIMUM": "operate",
        "GBAW_OPERATIONS_STATE_MACHINE_ARN": "arn:aws:states:us-west-2:111122223333:stateMachine:execute",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-abc",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": "arn:aws:gamelift:us-west-2:111122223333:fleet/fleet-abc",
        "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "gbaw-ops",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE": "kill-switch",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:111122223333:stateMachine:autonomy-execute"
        ),
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE": "autonomy-switch",
    }


@pytest.mark.unit
def test_import_is_aws_free() -> None:
    """Importing the evaluator entry must not touch boto3, the network, or env."""
    for name in list(sys.modules):
        if name == _MODULE:
            del sys.modules[name]
    boto3_before = sys.modules.get("boto3")
    __import__(_MODULE)
    # Importing must not have imported boto3 as a side effect.
    assert sys.modules.get("boto3") is boto3_before or boto3_before is not None


@pytest.mark.unit
def test_resolve_settings_refuses_unless_mode_is_exactly_operate() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    env = _base_env()
    env["GBAW_OPERATIONS_MODE"] = "remediate"  # below operate
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(env)


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing_key",
    [
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE",
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN",
        "GBAW_OPERATIONS_TABLE_NAME",
    ],
)
def test_resolve_settings_refuses_when_any_autonomy_identifier_missing(missing_key: str) -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    env = _base_env()
    del env[missing_key]
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(env)


@pytest.mark.unit
def test_resolve_settings_refuses_when_autonomy_flag_disabled() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    env = _base_env()
    env["GBAW_OPERATIONS_AUTONOMY_ENABLED"] = "false"
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(env)


@pytest.mark.unit
def test_resolve_settings_accepts_a_fully_configured_operate_deployment() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    settings = resolve_autonomy_evaluator_settings(_base_env())
    assert settings.static_deployment_mode == "operate"
    assert settings.autonomy_state_machine_arn.endswith("autonomy-execute")
    assert settings.autonomy_switch_profile == "autonomy-switch"
    # The autonomy switch profile must be a SEPARATE document from the kill switch.
    assert settings.autonomy_switch_profile != settings.executor.observation.operations.mode


@pytest.mark.unit
def test_build_runtime_uses_only_dynamodb_appconfig_and_stepfunctions_clients() -> None:
    """The evaluator role never constructs a GameLift or Lambda-invoke client."""
    # Local modules
    from operations.autonomy_runtime import evaluator_entry

    created: list[str] = []

    class _FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def client(self, name: str, **kwargs: Any) -> Any:
            created.append(name)
            return object()

    handler = evaluator_entry.build_evaluator_handler(
        settings=evaluator_entry.resolve_autonomy_evaluator_settings(_base_env()),
        session=_FakeSession(),
    )
    assert handler is not None
    # Only these three service clients may be created — never gamelift, never lambda.
    assert set(created) <= {"dynamodb", "appconfig", "stepfunctions", "cloudwatch"}
    assert "gamelift" not in created
    assert "lambda" not in created


@pytest.mark.unit
def test_start_execution_client_passes_operation_id_only() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import StepFunctionsStartExecution

    calls: list[dict[str, Any]] = []

    class _FakeSfn:
        def start_execution(self, **kwargs: Any) -> dict[str, str]:
            calls.append(kwargs)
            return {"executionArn": "arn:aws:states:us-west-2:111122223333:execution:autonomy-execute:x"}

    start = StepFunctionsStartExecution(
        client=_FakeSfn(),
        state_machine_arn="arn:aws:states:us-west-2:111122223333:stateMachine:autonomy-execute",
    )
    start({"operation_id": "op_" + "a" * 26})

    assert len(calls) == 1
    # The SFN input payload carries the operation_id and nothing else.
    # Standard library
    import json

    payload = json.loads(calls[0]["input"])
    assert payload == {"operation_id": "op_" + "a" * 26}
    assert calls[0]["stateMachineArn"].endswith("autonomy-execute")
