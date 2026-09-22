"""AppConfig kill-switch publisher tests (issue #416, E4).

:class:`~operations.control.appconfig_publisher.AppConfigKillSwitchPublisher`
publishes a new kill-switch document to AWS AppConfig as a hosted configuration
version and starts a deployment. It selects the deployment strategy by intent:

* a **hard-down** change (turning any phase or the master switch OFF relative to
  the current document) MUST deploy with the *immediate* strategy so the safety
  reduction propagates as fast as possible; and
* a **normal** change (only turning things ON, or otherwise not reducing
  authority) deploys with the *gradual* strategy.

The publisher validates the document against the immutable kill-switch contract
before publishing (fail closed), and it never widens: it is the mechanism, the
authority decision is the service's.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID, default_safe_kill_switch
from operations.control.appconfig_publisher import AppConfigKillSwitchPublisher, PublisherError

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _document(*, enabled: bool, prepare: bool, dispatch: bool, execute: bool, version: int = 2) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "config_version": version,
        "issued_at": _z(_NOW),
        "not_after": _z(_NOW + timedelta(minutes=10)),
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": prepare, "dispatch": dispatch, "execute": execute}},
    }


class _FakeAppConfig:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []

    def create_hosted_configuration_version(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"VersionNumber": len(self.created)}

    def start_deployment(self, **kwargs: Any) -> dict[str, Any]:
        self.deployments.append(kwargs)
        return {"DeploymentNumber": len(self.deployments)}


def _publisher(client: _FakeAppConfig) -> AppConfigKillSwitchPublisher:
    return AppConfigKillSwitchPublisher(
        client=client,
        application_id="app-1",
        environment_id="env-1",
        configuration_profile_id="profile-1",
        gradual_strategy_id="strategy-gradual",
        immediate_strategy_id="strategy-immediate",
    )


def test_normal_enable_uses_gradual_strategy() -> None:
    client = _FakeAppConfig()
    publisher = _publisher(client)
    publisher.publish(document=_document(enabled=True, prepare=True, dispatch=True, execute=True), hard_down=False)
    assert client.created
    assert client.deployments[0]["DeploymentStrategyId"] == "strategy-gradual"


def test_hard_down_uses_immediate_strategy() -> None:
    client = _FakeAppConfig()
    publisher = _publisher(client)
    document = default_safe_kill_switch(
        config_version=3, issued_at=_z(_NOW), not_after=_z(_NOW + timedelta(minutes=10))
    )
    publisher.publish(document=document, hard_down=True)
    assert client.deployments[0]["DeploymentStrategyId"] == "strategy-immediate"


def test_publish_validates_document_first() -> None:
    client = _FakeAppConfig()
    publisher = _publisher(client)
    bad = _document(enabled=True, prepare=True, dispatch=True, execute=True)
    bad["capabilities"][CAPABILITY_ID]["unknown"] = True  # schema violation
    with pytest.raises(PublisherError):
        publisher.publish(document=bad, hard_down=False)
    assert not client.created  # nothing published on invalid input


def test_publish_targets_configured_ids() -> None:
    client = _FakeAppConfig()
    publisher = _publisher(client)
    publisher.publish(document=_document(enabled=True, prepare=True, dispatch=True, execute=True), hard_down=False)
    created = client.created[0]
    assert created["ApplicationId"] == "app-1"
    assert created["ConfigurationProfileId"] == "profile-1"
    deployment = client.deployments[0]
    assert deployment["ApplicationId"] == "app-1"
    assert deployment["EnvironmentId"] == "env-1"


def test_provider_failure_raises_publisher_error() -> None:
    client = _FakeAppConfig()

    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("appconfig unavailable")

    client.create_hosted_configuration_version = boom  # type: ignore[assignment]
    publisher = _publisher(client)
    with pytest.raises(PublisherError):
        publisher.publish(document=_document(enabled=True, prepare=True, dispatch=True, execute=True), hard_down=False)
