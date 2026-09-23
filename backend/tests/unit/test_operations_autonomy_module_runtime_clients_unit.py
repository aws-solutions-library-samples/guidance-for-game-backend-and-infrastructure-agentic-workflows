"""The module runtime constructs no GameLift / Lambda-invoke client (issue #439).

``build_module_runtime`` wires the trusted loaders (DynamoDB observation store,
DynamoDB policy loader, durable window-state loader) and the runtime handler.
Like the evaluator role generally, it may create ONLY DynamoDB, AppConfig, and
Step Functions clients — never a GameLift client (it holds no provider-write
credential) and never a Lambda-invoke client.
"""

from __future__ import annotations

# Standard library
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"


def _base_env() -> dict[str, str]:
    policy = load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")
    window = load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")
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
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": policy["target"]["fleet_id"],
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": "arn:aws:gamelift:us-west-2:111122223333:fleet/"
        + policy["target"]["fleet_id"],
        "GBAW_OPERATIONS_ENROLLED_LOCATION": policy["target"]["location"],
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "gbaw-ops",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE": "kill-switch",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:111122223333:stateMachine:autonomy-execute"
        ),
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE": "autonomy-switch",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID": policy["policy_id"],
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION": policy["policy_version"],
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH": policy["policy_hash"],
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID": window["state_id"],
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT": "subject.autonomy-agent",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT": "client.autonomy-runtime",
    }


@pytest.mark.unit
def test_build_module_runtime_uses_only_dynamodb_appconfig_and_stepfunctions() -> None:
    # Local modules
    from operations.autonomy_runtime import evaluator_entry

    created: list[str] = []

    class _FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def client(self, service_name: str, *args: Any, **kwargs: Any) -> Any:
            created.append(service_name)
            return object()

    runtime = evaluator_entry.build_module_runtime(
        settings=evaluator_entry.resolve_autonomy_evaluator_settings(_base_env()),
        session=_FakeSession(),
    )
    assert runtime is not None
    # Never a provider-write or invoke client.
    assert "gamelift" not in created
    assert "lambda" not in created
    # Only these three services may ever be constructed.
    assert set(created) <= {"dynamodb", "stepfunctions", "appconfig"}
