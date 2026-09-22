"""Idempotent AWS AppConfig kill-switch publisher (issue #416, E4).

The publisher creates one deterministically labelled hosted configuration
version per control ``config_version`` and starts at most one deployment for
that label. AWS AppConfig does not expose a generic client token for these APIs,
so lost-response recovery uses provider-side primitives instead:

* ``VersionLabel`` identifies the exact hosted document;
* ``LatestVersionNumber`` prevents a stale retry from creating another version;
* ``LatestDeploymentNumber`` prevents a stale retry from starting another
  deployment; and
* bounded list/get reconciliation verifies existing content, strategy, and
  deployment state before treating a retry as successful.

A hard-down uses the immediate strategy. Any enable/non-reduction uses the
normal gradual strategy. If bounded reconciliation cannot prove the provider
state, the publisher fails closed instead of repeating a potentially completed
side effect.
"""

from __future__ import annotations

# Standard library
import json
from collections.abc import Mapping
from typing import Any, Protocol

# Local modules
from operations.contracts.control_plane import KILL_SWITCH_SCHEMA_NAME, ControlContractError, validate_control_contract

_CONTENT_TYPE = "application/json"
_MAX_LIST_PAGES = 20
_PAGE_SIZE = 50
_IN_PROGRESS_OR_COMPLETE = frozenset({"VALIDATING", "DEPLOYING", "BAKING", "COMPLETE"})
_ROLLBACK_STATES = frozenset({"ROLLING_BACK", "ROLLED_BACK", "REVERTED"})


class PublisherError(RuntimeError):
    """Publishing or reconciling the kill-switch document failed closed."""


class AppConfigClientPort(Protocol):
    """The narrow slice of the boto3 AppConfig client this publisher uses."""

    def list_hosted_configuration_versions(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_hosted_configuration_version(self, **kwargs: Any) -> dict[str, Any]: ...

    def create_hosted_configuration_version(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_deployments(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_deployment(self, **kwargs: Any) -> dict[str, Any]: ...

    def start_deployment(self, **kwargs: Any) -> dict[str, Any]: ...


class AppConfigKillSwitchPublisher:
    """Publish one validated document without duplicating provider effects."""

    def __init__(
        self,
        *,
        client: AppConfigClientPort,
        application_id: str,
        environment_id: str,
        configuration_profile_id: str,
        gradual_strategy_id: str,
        immediate_strategy_id: str,
    ) -> None:
        for name, value in (
            ("application_id", application_id),
            ("environment_id", environment_id),
            ("configuration_profile_id", configuration_profile_id),
            ("gradual_strategy_id", gradual_strategy_id),
            ("immediate_strategy_id", immediate_strategy_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._client = client
        self._application_id = application_id
        self._environment_id = environment_id
        self._configuration_profile_id = configuration_profile_id
        self._gradual_strategy_id = gradual_strategy_id
        self._immediate_strategy_id = immediate_strategy_id

    def publish(self, *, document: dict[str, Any], hard_down: bool) -> None:
        """Validate and idempotently publish ``document``.

        The deterministic label and AppConfig optimistic-lock parameters make a
        lost response reconcilable. A retry never blindly repeats either
        provider side effect.
        """
        try:
            validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)
        except ControlContractError as exc:
            raise PublisherError("kill-switch document failed its contract") from exc

        content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        label = f"gbaw-control-v{int(document['config_version'])}"
        strategy_id = self._immediate_strategy_id if hard_down else self._gradual_strategy_id

        self._ensure_hosted_version(label=label, content=content)
        self._ensure_deployment(label=label, strategy_id=strategy_id)

    def _ensure_hosted_version(self, *, label: str, content: bytes) -> int:
        existing = self._find_hosted_version(label=label, expected_content=content)
        if existing is not None:
            return existing

        inventory = self._list_hosted_versions()
        latest = max((_positive_int(item.get("VersionNumber")) or 0 for item in inventory), default=0)
        request: dict[str, Any] = {
            "ApplicationId": self._application_id,
            "ConfigurationProfileId": self._configuration_profile_id,
            "Content": content,
            "ContentType": _CONTENT_TYPE,
            "VersionLabel": label,
            "Description": f"Game Agent operations control {label}",
        }
        if latest > 0:
            request["LatestVersionNumber"] = latest

        try:
            created = self._client.create_hosted_configuration_version(**request)
        except Exception as exc:  # noqa: BLE001 - reconcile a possibly lost response
            reconciled = self._find_hosted_version(label=label, expected_content=content)
            if reconciled is not None:
                return reconciled
            raise PublisherError("could not create or reconcile the hosted kill-switch version") from exc

        version_number = _positive_int(created.get("VersionNumber")) if isinstance(created, Mapping) else None
        if version_number is None:
            raise PublisherError("AppConfig did not return a hosted configuration version number")
        self._verify_hosted_content(version_number=version_number, label=label, expected_content=content)
        return version_number

    def _find_hosted_version(self, *, label: str, expected_content: bytes) -> int | None:
        items = self._list_hosted_versions(version_label=label)
        if len(items) > 1:
            raise PublisherError("multiple hosted versions use the deterministic control label")
        if not items:
            return None
        version_number = _positive_int(items[0].get("VersionNumber"))
        if version_number is None:
            raise PublisherError("the labelled hosted version has no valid version number")
        self._verify_hosted_content(
            version_number=version_number,
            label=label,
            expected_content=expected_content,
        )
        return version_number

    def _verify_hosted_content(self, *, version_number: int, label: str, expected_content: bytes) -> None:
        try:
            response = self._client.get_hosted_configuration_version(
                ApplicationId=self._application_id,
                ConfigurationProfileId=self._configuration_profile_id,
                VersionNumber=version_number,
            )
            raw = response.get("Content") if isinstance(response, Mapping) else None
            content = raw.read() if hasattr(raw, "read") else raw
        except Exception as exc:  # noqa: BLE001
            raise PublisherError("could not verify the hosted kill-switch version") from exc
        if not isinstance(content, (bytes, bytearray)) or bytes(content) != expected_content:
            raise PublisherError("the deterministic control label is bound to different content")
        response_label = response.get("VersionLabel") if isinstance(response, Mapping) else None
        if response_label is not None and response_label != label:
            raise PublisherError("the hosted kill-switch version label does not match")

    def _ensure_deployment(self, *, label: str, strategy_id: str) -> None:
        existing, latest = self._deployment_inventory(label=label, strategy_id=strategy_id)
        if existing:
            return

        request: dict[str, Any] = {
            "ApplicationId": self._application_id,
            "EnvironmentId": self._environment_id,
            "ConfigurationProfileId": self._configuration_profile_id,
            "ConfigurationVersion": label,
            "DeploymentStrategyId": strategy_id,
            "Description": f"Game Agent operations control {label}",
        }
        if latest > 0:
            request["LatestDeploymentNumber"] = latest
        try:
            self._client.start_deployment(**request)
        except Exception as exc:  # noqa: BLE001 - reconcile a possibly lost response
            reconciled, _ = self._deployment_inventory(label=label, strategy_id=strategy_id)
            if reconciled:
                return
            raise PublisherError("could not start or reconcile the kill-switch deployment") from exc

    def _deployment_inventory(self, *, label: str, strategy_id: str) -> tuple[bool, int]:
        items = self._list_deployments()
        latest = max((_positive_int(item.get("DeploymentNumber")) or 0 for item in items), default=0)
        candidates = [
            item
            for item in items
            if item.get("ConfigurationProfileId") == self._configuration_profile_id
            and (item.get("VersionLabel") == label or item.get("ConfigurationVersion") == label)
        ]
        if len(candidates) > 1:
            raise PublisherError("multiple deployments exist for one deterministic control label")
        if not candidates:
            return False, latest

        number = _positive_int(candidates[0].get("DeploymentNumber"))
        if number is None:
            raise PublisherError("the matching deployment has no valid deployment number")
        try:
            deployment = self._client.get_deployment(
                ApplicationId=self._application_id,
                EnvironmentId=self._environment_id,
                DeploymentNumber=number,
            )
        except Exception as exc:  # noqa: BLE001
            raise PublisherError("could not verify the matching kill-switch deployment") from exc
        if deployment.get("ConfigurationProfileId") != self._configuration_profile_id:
            raise PublisherError("the matching deployment targets a different configuration profile")
        if deployment.get("VersionLabel") != label and deployment.get("ConfigurationVersion") != label:
            raise PublisherError("the matching deployment targets a different configuration version")
        if deployment.get("DeploymentStrategyId") != strategy_id:
            raise PublisherError("the matching deployment used a different strategy")
        state = deployment.get("State")
        if state in _IN_PROGRESS_OR_COMPLETE:
            return True, latest
        if state in _ROLLBACK_STATES:
            raise PublisherError("the matching kill-switch deployment rolled back")
        raise PublisherError("the matching kill-switch deployment has an unknown state")

    def _list_hosted_versions(self, *, version_label: str | None = None) -> list[dict[str, Any]]:
        request: dict[str, Any] = {
            "ApplicationId": self._application_id,
            "ConfigurationProfileId": self._configuration_profile_id,
            "MaxResults": _PAGE_SIZE,
        }
        if version_label is not None:
            request["VersionLabel"] = version_label
        return self._bounded_list(self._client.list_hosted_configuration_versions, request)

    def _list_deployments(self) -> list[dict[str, Any]]:
        return self._bounded_list(
            self._client.list_deployments,
            {
                "ApplicationId": self._application_id,
                "EnvironmentId": self._environment_id,
                "MaxResults": _PAGE_SIZE,
            },
        )

    @staticmethod
    def _bounded_list(operation: Any, request: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            page_request = dict(request)
            if next_token is not None:
                page_request["NextToken"] = next_token
            try:
                response = operation(**page_request)
            except Exception as exc:  # noqa: BLE001
                raise PublisherError("AppConfig reconciliation list failed") from exc
            page_items = response.get("Items", []) if isinstance(response, Mapping) else []
            if not isinstance(page_items, list) or any(not isinstance(item, dict) for item in page_items):
                raise PublisherError("AppConfig reconciliation returned malformed items")
            items.extend(page_items)
            token = response.get("NextToken") if isinstance(response, Mapping) else None
            if token is None:
                return items
            if not isinstance(token, str) or not token:
                raise PublisherError("AppConfig reconciliation returned an invalid continuation token")
            next_token = token
        raise PublisherError("AppConfig reconciliation exceeded the bounded page limit")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
