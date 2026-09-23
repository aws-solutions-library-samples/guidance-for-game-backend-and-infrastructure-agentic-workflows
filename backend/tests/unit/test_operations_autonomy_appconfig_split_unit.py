"""E5 dual-AppConfig contract tests (#440, Finding 3).

The evaluator composes TWO independent AppConfig documents:

* the SEPARATE E5 autonomy switch, in its OWN 09-stack AppConfig application /
  environment (``GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION`` /
  ``..._ENVIRONMENT``), read under ``GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE``;
* the EXISTING E4 kill switch, in the 08-stack kill-switch AppConfig application
  / environment (``GBAW_OPERATIONS_APPCONFIG_APPLICATION`` / ``..._ENVIRONMENT``),
  read under ``GBAW_OPERATIONS_APPCONFIG_PROFILE``.

Before this fix both documents were forced into ONE application/environment, so
the evaluator could not address the E4 kill switch that lives in its own 08
application. These tests lock the split: enabling E4 never enables autonomy and
the evaluator can read BOTH switches from BOTH applications.
"""

from __future__ import annotations

# Standard library
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
        # E4 kill-switch application/environment/profile (08 stack).
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "gbaw-ops-control",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE": "kill-switch",
        # SEPARATE E5 autonomy application/environment (09 stack).
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION": "gbaw-ops-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT": "prod",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:111122223333:stateMachine:autonomy-execute"
        ),
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE": "autonomy-switch",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID": "policy.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION": "2026-09-01",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH": "sha256:" + "7" * 64,
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID": "state.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT": "subject.autonomy-agent",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT": "client.autonomy-runtime",
    }


@pytest.mark.unit
def test_settings_expose_separate_autonomy_and_kill_switch_applications() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    settings = resolve_autonomy_evaluator_settings(_base_env())

    # E4 kill-switch application/environment stays the 08-stack pair.
    assert settings.appconfig_application == "gbaw-ops-control"
    assert settings.appconfig_environment == "prod"
    # E5 autonomy application/environment is a DIFFERENT application.
    assert settings.autonomy_appconfig_application == "gbaw-ops-autonomy"
    assert settings.autonomy_appconfig_environment == "prod"
    assert settings.autonomy_appconfig_application != settings.appconfig_application


@pytest.mark.unit
def test_settings_refuse_when_autonomy_application_missing() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    env = _base_env()
    del env["GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION"]
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(env)


@pytest.mark.unit
def test_bootstrap_reads_each_switch_from_its_own_application(monkeypatch: pytest.MonkeyPatch) -> None:
    """The autonomy switch reads the autonomy app; the kill switch reads the E4 app."""
    # Local modules
    from operations.autonomy_runtime import evaluator_entry
    from operations.control import appconfig_extension

    captured: list[dict[str, str]] = []
    real_init = appconfig_extension.AppConfigExtensionClient.__init__

    def _spy_init(self: Any, **kwargs: Any) -> None:  # noqa: ANN401
        captured.append(
            {
                "application": kwargs["application"],
                "environment": kwargs["environment"],
                "profile": kwargs["profile"],
            }
        )
        real_init(self, **kwargs)

    monkeypatch.setattr(appconfig_extension.AppConfigExtensionClient, "__init__", _spy_init)

    class _FakeSession:
        def client(self, name: str, **kwargs: Any) -> Any:  # noqa: ANN401
            return object()

    handler = evaluator_entry.build_evaluator_handler(
        settings=evaluator_entry.resolve_autonomy_evaluator_settings(_base_env()),
        session=_FakeSession(),
    )
    assert handler is not None

    by_profile = {c["profile"]: c for c in captured}
    # The autonomy switch document lives in the SEPARATE autonomy application.
    assert by_profile["autonomy-switch"]["application"] == "gbaw-ops-autonomy"
    # The E4 kill switch document lives in the 08 kill-switch application.
    assert by_profile["kill-switch"]["application"] == "gbaw-ops-control"
