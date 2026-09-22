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


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content


class _FakeAppConfig:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []

    def list_hosted_configuration_versions(self, **kwargs: Any) -> dict[str, Any]:
        label = kwargs.get("VersionLabel")
        items = [
            {
                "VersionNumber": index,
                "VersionLabel": created.get("VersionLabel"),
                "ContentType": created["ContentType"],
            }
            for index, created in enumerate(self.created, start=1)
            if label is None or created.get("VersionLabel") == label
        ]
        return {"Items": items}

    def get_hosted_configuration_version(self, **kwargs: Any) -> dict[str, Any]:
        version = int(kwargs["VersionNumber"])
        created = self.created[version - 1]
        return {"Content": _FakeBody(created["Content"]), "VersionLabel": created.get("VersionLabel")}

    def create_hosted_configuration_version(self, **kwargs: Any) -> dict[str, Any]:
        latest = kwargs.get("LatestVersionNumber")
        if latest is not None and latest != len(self.created):
            raise RuntimeError("hosted version conflict")
        if any(item.get("VersionLabel") == kwargs.get("VersionLabel") for item in self.created):
            raise RuntimeError("duplicate version label")
        self.created.append(kwargs)
        return {"VersionNumber": len(self.created), "VersionLabel": kwargs.get("VersionLabel")}

    def list_deployments(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Items": [
                {
                    "DeploymentNumber": index,
                    "ConfigurationProfileId": item["ConfigurationProfileId"],
                    "ConfigurationVersion": item["ConfigurationVersion"],
                    "VersionLabel": item["ConfigurationVersion"],
                    "State": "COMPLETE",
                }
                for index, item in reversed(list(enumerate(self.deployments, start=1)))
            ]
        }

    def get_deployment(self, **kwargs: Any) -> dict[str, Any]:
        item = self.deployments[int(kwargs["DeploymentNumber"]) - 1]
        return {
            "DeploymentNumber": int(kwargs["DeploymentNumber"]),
            "ConfigurationProfileId": item["ConfigurationProfileId"],
            "ConfigurationVersion": item["ConfigurationVersion"],
            "VersionLabel": item["ConfigurationVersion"],
            "DeploymentStrategyId": item["DeploymentStrategyId"],
            "State": "COMPLETE",
        }

    def start_deployment(self, **kwargs: Any) -> dict[str, Any]:
        latest = kwargs.get("LatestDeploymentNumber")
        if latest is not None and latest != len(self.deployments):
            raise RuntimeError("deployment conflict")
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


def test_provider_failure_raises_publisher_error(caplog: pytest.LogCaptureFixture) -> None:
    client = _FakeAppConfig()

    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider-secret-detail")

    client.create_hosted_configuration_version = boom  # type: ignore[assignment]
    publisher = _publisher(client)
    with pytest.raises(PublisherError):
        publisher.publish(document=_document(enabled=True, prepare=True, dispatch=True, execute=True), hard_down=False)
    assert "create_hosted_version" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "provider-secret-detail" not in caplog.text


def test_repeated_publish_reuses_one_hosted_version_and_one_deployment() -> None:
    client = _FakeAppConfig()
    publisher = _publisher(client)
    document = _document(enabled=True, prepare=True, dispatch=True, execute=False, version=17)
    publisher.publish(document=document, hard_down=False)
    publisher.publish(document=document, hard_down=False)
    assert len(client.created) == 1
    assert client.created[0]["VersionLabel"] == "gbaw-control-v17"
    assert len(client.deployments) == 1


def test_lost_create_response_reconciles_by_deterministic_label() -> None:
    client = _FakeAppConfig()
    original = client.create_hosted_configuration_version
    lost_once = True

    def lose_response(**kwargs: Any) -> dict[str, Any]:
        nonlocal lost_once
        response = original(**kwargs)
        if lost_once:
            lost_once = False
            raise RuntimeError("response lost after create")
        return response

    client.create_hosted_configuration_version = lose_response  # type: ignore[assignment]
    publisher = _publisher(client)
    publisher.publish(
        document=_document(enabled=True, prepare=True, dispatch=False, execute=False, version=18),
        hard_down=False,
    )
    assert len(client.created) == 1
    assert len(client.deployments) == 1


def test_lost_start_response_reconciles_without_second_deployment() -> None:
    client = _FakeAppConfig()
    original = client.start_deployment
    lost_once = True

    def lose_response(**kwargs: Any) -> dict[str, Any]:
        nonlocal lost_once
        response = original(**kwargs)
        if lost_once:
            lost_once = False
            raise RuntimeError("response lost after start")
        return response

    client.start_deployment = lose_response  # type: ignore[assignment]
    publisher = _publisher(client)
    publisher.publish(
        document=_document(enabled=False, prepare=False, dispatch=False, execute=False, version=19),
        hard_down=True,
    )
    assert len(client.created) == 1
    assert len(client.deployments) == 1
    assert client.deployments[0]["DeploymentStrategyId"] == "strategy-immediate"


def test_legacy_pending_marker_reconciles_the_existing_validated_hosted_version() -> None:
    client = _FakeAppConfig()
    document = _document(enabled=True, prepare=True, dispatch=False, execute=False, version=23)
    content = __import__("json").dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    client.create_hosted_configuration_version(
        ApplicationId="app-1",
        ConfigurationProfileId="profile-1",
        Content=content,
        ContentType="application/json",
        VersionLabel="gbaw-control-v23",
    )
    publisher = _publisher(client)
    recovered = publisher.reconcile_existing(
        config_version=23,
        desired={
            "operations_enabled": True,
            "capabilities": {CAPABILITY_ID: {"prepare": True, "dispatch": False, "execute": False}},
        },
        hard_down=False,
    )
    assert recovered == document
    assert len(client.created) == 1
    assert len(client.deployments) == 1


def test_legacy_reconciliation_refuses_hosted_content_with_different_authority() -> None:
    client = _FakeAppConfig()
    document = _document(enabled=True, prepare=True, dispatch=True, execute=False, version=24)
    content = __import__("json").dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    client.create_hosted_configuration_version(
        ApplicationId="app-1",
        ConfigurationProfileId="profile-1",
        Content=content,
        ContentType="application/json",
        VersionLabel="gbaw-control-v24",
    )
    publisher = _publisher(client)
    with pytest.raises(PublisherError):
        publisher.reconcile_existing(
            config_version=24,
            desired={
                "operations_enabled": False,
                "capabilities": {CAPABILITY_ID: {"prepare": False, "dispatch": False, "execute": False}},
            },
            hard_down=True,
        )
    assert client.deployments == []
