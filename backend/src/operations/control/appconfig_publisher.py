"""AppConfig kill-switch publisher (issue #416, E4).

:class:`AppConfigKillSwitchPublisher` is the mechanism that publishes a new
kill-switch document to AWS AppConfig: it creates a hosted configuration version
and starts a deployment. It is deliberately mechanism-only — the authority
decision (what booleans are allowed, the compare-and-set) lives in the control
service; the publisher just puts the already-decided document live.

Strategy selection by intent
----------------------------
The publisher chooses the AppConfig deployment strategy from a single
``hard_down`` flag the caller sets:

* **hard-down** (turning the master switch or any phase OFF — a safety
  reduction) deploys with the *immediate* strategy so the reduction propagates
  as fast as AppConfig allows; and
* **normal** (turning things on, or otherwise not reducing authority) deploys
  with the *gradual* strategy, so an enable rolls out progressively.

The document is validated against the immutable ``operations-kill-switch``
contract before anything is published (fail closed): a malformed document is
never put live. Any AppConfig provider failure is surfaced as a bounded
:class:`PublisherError` — never a partial or silent publish.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any, Protocol

# Local modules
from operations.contracts.control_plane import (
    KILL_SWITCH_SCHEMA_NAME,
    ControlContractError,
    validate_control_contract,
)

_CONTENT_TYPE = "application/json"


class PublisherError(RuntimeError):
    """Publishing the kill-switch document to AppConfig failed (fail closed)."""


class AppConfigClientPort(Protocol):
    """The narrow slice of the boto3 AppConfig client this publisher uses."""

    def create_hosted_configuration_version(self, **kwargs: Any) -> dict[str, Any]: ...

    def start_deployment(self, **kwargs: Any) -> dict[str, Any]: ...


class AppConfigKillSwitchPublisher:
    """Publish a validated kill-switch document, immediate on a hard-down."""

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
        """Validate and publish ``document``; immediate strategy on a hard-down."""
        try:
            validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)
        except ControlContractError as exc:
            raise PublisherError("kill-switch document failed its contract") from exc

        content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        strategy_id = self._immediate_strategy_id if hard_down else self._gradual_strategy_id

        try:
            created = self._client.create_hosted_configuration_version(
                ApplicationId=self._application_id,
                ConfigurationProfileId=self._configuration_profile_id,
                Content=content,
                ContentType=_CONTENT_TYPE,
            )
            version_number = created.get("VersionNumber")
            self._client.start_deployment(
                ApplicationId=self._application_id,
                EnvironmentId=self._environment_id,
                ConfigurationProfileId=self._configuration_profile_id,
                ConfigurationVersion=str(version_number),
                DeploymentStrategyId=strategy_id,
            )
        except PublisherError:
            raise
        except Exception as exc:  # noqa: BLE001 - any provider failure is a bounded PublisherError
            raise PublisherError("could not publish the kill-switch document") from exc
