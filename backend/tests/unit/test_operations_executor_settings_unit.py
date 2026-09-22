"""E3 executor deployment settings tests (#415)."""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.settings import resolve_executor_deployment_settings

_BASE = {
    "GBAW_OPERATIONS_MODE": "remediate",
    "GBAW_OPERATIONS_TABLE_NAME": "ops",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "aud",
    "GBAW_OPERATIONS_STATE_MACHINE_ARN": "arn:aws:states:us-west-2:1:stateMachine:x",
    "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-1234abcd",
    "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": "arn:aws:gamelift:us-west-2:1:fleet/fleet-1234abcd",
    "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
}


def test_remediate_deployment_is_execute_enabled() -> None:
    settings = resolve_executor_deployment_settings(dict(_BASE))
    assert settings.execute_enabled is True
    assert settings.capability_maximum == "remediate"


def test_below_remediate_mode_is_not_execute_enabled() -> None:
    env = dict(_BASE, GBAW_OPERATIONS_MODE="advise")
    assert resolve_executor_deployment_settings(env).execute_enabled is False


def test_capability_below_remediate_is_not_execute_enabled() -> None:
    env = dict(_BASE, GBAW_OPERATIONS_CAPABILITY_MAXIMUM="advise")
    assert resolve_executor_deployment_settings(env).execute_enabled is False


@pytest.mark.parametrize(
    "missing",
    [
        "GBAW_OPERATIONS_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN",
        "GBAW_OPERATIONS_ENROLLED_LOCATION",
    ],
)
def test_missing_required_fails_closed(missing: str) -> None:
    env = {k: v for k, v in _BASE.items() if k != missing}
    with pytest.raises(ValueError):
        resolve_executor_deployment_settings(env)


def test_non_gamelift_arn_rejected() -> None:
    env = dict(_BASE, GBAW_OPERATIONS_ENROLLED_FLEET_ARN="arn:aws:ec2:us-west-2:1:instance/i-1")
    with pytest.raises(ValueError):
        resolve_executor_deployment_settings(env)
