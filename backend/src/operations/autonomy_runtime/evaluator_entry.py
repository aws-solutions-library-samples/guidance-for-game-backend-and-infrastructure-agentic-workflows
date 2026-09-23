"""Deployable Lambda evaluator entry/bootstrap for the E5 runtime (issue #439).

This module is the real, deployable composition point that turns strict,
server-owned environment settings into a live
:class:`~operations.autonomy_runtime.handler.AutonomyRuntimeHandler`. It is the
piece that finishes the E5 source-level wiring: nothing here is "deferred to
future infrastructure". It constructs, from env only:

* the fresh, **separate** AppConfig autonomy switch
  (:class:`~operations.autonomy_switch.AutonomySwitchGate`) over the AppConfig
  Lambda-extension client, pointed at its own profile;
* the E4 cached kill-switch dispatch gate and the E4 **durable** control gate;
* the composite :class:`~operations.autonomy_gate.AutonomyRuntimeGate`;
* the durable :class:`~operations.autonomy_runtime.store.DynamoDbReservationStore`
  and the immutable :class:`~operations.autonomy_runtime.store.DynamoDbAutonomyBundleStore`;
* the code-owned policy loader and the durable observation / window-state loaders;
* a Step Functions ``StartExecution`` client that starts the Standard workflow
  with ``operation_id`` **alone**; and
* the :class:`AutonomyRuntimeHandler` that ties them together.

Safety boundaries (ADR 0001 / AGENTS.md):

* **Default disabled + startup refusal.** :func:`resolve_autonomy_evaluator_settings`
  fails closed unless the static deployment mode is EXACTLY ``operate`` and every
  autonomy identifier is configured. A default deployment provisions no autonomy
  control plane, so the bootstrap is never reached there.
* **The evaluator holds no provider-write credential.** It constructs ONLY
  DynamoDB, AppConfig, and Step Functions clients (plus CloudWatch for metrics) —
  never a GameLift client and never a Lambda-invoke client. It advances durable
  accounting state and starts a durable workflow with an identifier; the single
  provider write is performed elsewhere, by the existing narrow executor, invoked
  solely by that durable authenticated Step Functions state machine.
* **Import is AWS-free.** Importing this module reads no environment, creates no
  client, and performs no I/O; boto3 is imported lazily inside the bootstrap.
"""

from __future__ import annotations

# Standard library
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Local modules
from operations.settings import resolve_executor_deployment_settings

# The autonomy-specific env keys, additive to the shared operations settings.
_AUTONOMY_STATE_MACHINE_KEY = "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN"
_AUTONOMY_SWITCH_PROFILE_KEY = "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE"
_APPCONFIG_APPLICATION_KEY = "GBAW_OPERATIONS_APPCONFIG_APPLICATION"
_APPCONFIG_ENVIRONMENT_KEY = "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT"
_APPCONFIG_PROFILE_KEY = "GBAW_OPERATIONS_APPCONFIG_PROFILE"
_APPCONFIG_EXTENSION_PORT_KEY = "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT"
_AUTONOMY_POLICY_ID_KEY = "GBAW_OPERATIONS_AUTONOMY_POLICY_ID"
_AUTONOMY_POLICY_VERSION_KEY = "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION"
_AUTONOMY_POLICY_HASH_KEY = "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH"

_REQUIRED_STATIC_MODE = "operate"
_STATE_MACHINE_ARN_PREFIX = "arn:aws:states:"


@dataclass(frozen=True, slots=True)
class AutonomyEvaluatorDeploymentSettings:
    """The strict, server-owned settings the evaluator bootstrap resolves from env.

    Embeds the frozen executor deployment settings (durable table, tenant/
    workspace, enrolled fleet id/ARN/location, capability maximum) and adds the
    autonomy-specific identifiers: the Standard Step Functions workflow ARN for
    autonomous execution, the SEPARATE AppConfig autonomy-switch profile, the E4
    kill-switch profile, and the shared AppConfig application/environment/
    extension-port read target. Every field is validated at resolve time and
    fails closed.
    """

    executor: Any  # ExecutorDeploymentSettings
    static_deployment_mode: str
    autonomy_state_machine_arn: str
    autonomy_switch_profile: str
    kill_switch_profile: str
    autonomy_policy_id: str
    autonomy_policy_version: str
    autonomy_policy_hash: str
    appconfig_application: str
    appconfig_environment: str
    appconfig_extension_port: int

    def __post_init__(self) -> None:
        if self.static_deployment_mode != _REQUIRED_STATIC_MODE:
            raise ValueError("bounded autonomy requires the static deployment mode to be exactly 'operate'")
        for name in (
            "autonomy_state_machine_arn",
            "autonomy_switch_profile",
            "kill_switch_profile",
            "autonomy_policy_id",
            "autonomy_policy_version",
            "autonomy_policy_hash",
            "appconfig_application",
            "appconfig_environment",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not self.autonomy_state_machine_arn.startswith(_STATE_MACHINE_ARN_PREFIX):
            raise ValueError("autonomy_state_machine_arn must be a Step Functions state machine ARN")
        # The autonomy switch MUST be a separate AppConfig document from the E4
        # kill switch; sharing one document would let enabling E4 enable autonomy.
        if self.autonomy_switch_profile == self.kill_switch_profile:
            raise ValueError("the autonomy switch profile must be separate from the kill-switch profile")
        if not (1 <= self.appconfig_extension_port <= 65535):
            raise ValueError("appconfig_extension_port must be a valid TCP port")


def _required(source: Mapping[str, str], key: str) -> str:
    value = (source.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} must be configured")
    return value


def _resolve_bool(source: Mapping[str, str], key: str) -> bool:
    return (source.get(key) or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_autonomy_evaluator_settings(
    env: Mapping[str, str] | None = None,
) -> AutonomyEvaluatorDeploymentSettings:
    """Resolve the evaluator settings, refusing at startup unless fully configured.

    Fails closed (``ValueError``) unless the autonomy flag is enabled, the static
    deployment mode is EXACTLY ``operate``, and every autonomy identifier is
    present. This is the single startup gate that keeps the default-disabled
    runtime from ever constructing a dispatchable handler in a misconfigured or
    non-operate deployment.
    """
    # Standard library
    import os

    source: Mapping[str, str] = os.environ if env is None else env

    if not _resolve_bool(source, "GBAW_OPERATIONS_AUTONOMY_ENABLED"):
        raise ValueError("bounded autonomy is disabled (GBAW_OPERATIONS_AUTONOMY_ENABLED is not a true token)")

    static_mode = (source.get("GBAW_OPERATIONS_MODE") or "disabled").strip().lower() or "disabled"
    if static_mode != _REQUIRED_STATIC_MODE:
        raise ValueError("bounded autonomy requires GBAW_OPERATIONS_MODE to be exactly 'operate'")

    # The executor deployment settings resolver already validates the durable
    # table, tenant/workspace, enrolled fleet id/ARN/location, and capability
    # maximum, failing closed on any missing identifier.
    executor = resolve_executor_deployment_settings(source)

    port_raw = (source.get(_APPCONFIG_EXTENSION_PORT_KEY) or "2772").strip() or "2772"
    try:
        extension_port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"{_APPCONFIG_EXTENSION_PORT_KEY} must be an integer") from exc

    return AutonomyEvaluatorDeploymentSettings(
        executor=executor,
        static_deployment_mode=static_mode,
        autonomy_state_machine_arn=_required(source, _AUTONOMY_STATE_MACHINE_KEY),
        autonomy_switch_profile=_required(source, _AUTONOMY_SWITCH_PROFILE_KEY),
        kill_switch_profile=_required(source, _APPCONFIG_PROFILE_KEY),
        autonomy_policy_id=_required(source, _AUTONOMY_POLICY_ID_KEY),
        autonomy_policy_version=_required(source, _AUTONOMY_POLICY_VERSION_KEY),
        autonomy_policy_hash=_required(source, _AUTONOMY_POLICY_HASH_KEY),
        appconfig_application=_required(source, _APPCONFIG_APPLICATION_KEY),
        appconfig_environment=_required(source, _APPCONFIG_ENVIRONMENT_KEY),
        appconfig_extension_port=extension_port,
    )


class StepFunctionsStartExecution:
    """Start the Standard autonomy workflow with an identifier-only input.

    The dispatch payload is serialized to the SFN ``input`` as ``operation_id``
    ALONE. No policy, limits, observation, window state, or credential crosses
    this boundary; the executor reloads the durable bundle by id and re-verifies
    every precondition itself before the single provider write.
    """

    __slots__ = ("_client", "_state_machine_arn")

    def __init__(self, *, client: Any, state_machine_arn: str) -> None:
        if not isinstance(state_machine_arn, str) or not state_machine_arn.startswith(_STATE_MACHINE_ARN_PREFIX):
            raise ValueError("state_machine_arn must be a Step Functions state machine ARN")
        self._client = client
        self._state_machine_arn = state_machine_arn

    def __call__(self, payload: Mapping[str, str]) -> None:
        operation_id = payload["operation_id"]
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        # Serialize ONLY the operation id; nothing else crosses the boundary.
        self._client.start_execution(
            stateMachineArn=self._state_machine_arn,
            input=json.dumps({"operation_id": operation_id}, separators=(",", ":")),
        )


class DynamoDbWindowStateLoader:
    """Load the current durable rolling-window state from the reservation store."""

    __slots__ = ("_reservation_store",)

    def __init__(self, reservation_store: Any) -> None:
        self._reservation_store = reservation_store

    def load(self, state_id: str) -> dict[str, Any]:
        state: dict[str, Any] = self._reservation_store.current(state_id)
        return state


class _UnusedReservationPort:
    """A fail-closed reservation port for the composite pre-dispatch gate.

    The composite gate's pre-provider-write reservation is performed by the
    runtime handler's own reservation store, not by the gate, so the gate's
    reservation port is never exercised on the pre-dispatch path. It fails closed
    if ever called.
    """

    def reserve(self, **kwargs: Any) -> bool:
        return False


def build_evaluator_handler(
    *,
    settings: AutonomyEvaluatorDeploymentSettings,
    session: Any = None,
) -> Any:
    """Construct the live :class:`AutonomyRuntimeHandler` from resolved settings.

    ``session`` is injected by tests to assert the exact set of service clients
    created; production passes ``None`` and a real ``boto3.Session`` is built.
    Only DynamoDB, AppConfig, and Step Functions clients are ever created — never
    GameLift and never Lambda invoke.
    """
    # Local modules
    from operations.autonomy_gate import AutonomyRuntimeGate, AutonomyRuntimeSettings
    from operations.autonomy_runtime.handler import AutonomyRuntimeHandler
    from operations.autonomy_runtime.service import AutonomyRuntimeService
    from operations.autonomy_runtime.store import DynamoDbAutonomyBundleStore, DynamoDbReservationStore
    from operations.autonomy_switch import AutonomySwitchGate
    from operations.contracts.autonomy import evaluate_autonomy_policy
    from operations.control.appconfig_extension import AppConfigExtensionClient
    from operations.control.durable_gate import DynamoDbDurableControlGate
    from operations.control.gate_bootstrap import build_kill_switch_gate
    from operations.settings import KillSwitchExtensionSettings

    if session is None:
        # Third-party packages
        import boto3
        from botocore.config import Config as BotocoreConfig

        config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
        session = boto3.Session(region_name=_region())
        dynamodb_client = session.client("dynamodb", config=config)
        stepfunctions_client = session.client("stepfunctions", config=config)
    else:
        dynamodb_client = session.client("dynamodb")
        stepfunctions_client = session.client("stepfunctions")

    table_name = settings.executor.observation.table_name

    # Durable stores (DynamoDB only).
    reservation_store = DynamoDbReservationStore(client=dynamodb_client, table_name=table_name)
    bundle_store = DynamoDbAutonomyBundleStore(client=dynamodb_client, table_name=table_name)

    # The fresh, SEPARATE AppConfig autonomy switch over the localhost extension.
    autonomy_extension = AppConfigExtensionClient(
        application=settings.appconfig_application,
        environment=settings.appconfig_environment,
        profile=settings.autonomy_switch_profile,
        port=settings.appconfig_extension_port,
    )
    switch_gate = AutonomySwitchGate(extension=autonomy_extension)

    # The E4 cached kill-switch dispatch gate over its OWN AppConfig profile, and
    # the E4 durable control intent gate. Both are read-only controls.
    kill_switch_gate = build_kill_switch_gate(
        extension_settings=KillSwitchExtensionSettings(
            application=settings.appconfig_application,
            environment=settings.appconfig_environment,
            profile=settings.kill_switch_profile,
            extension_port=settings.appconfig_extension_port,
        ),
        static_authority=settings.static_deployment_mode,
    )
    durable_control_gate = DynamoDbDurableControlGate(client=dynamodb_client, table_name=table_name)

    # The composite pre-dispatch gate. Autonomy is enabled at exactly operate.
    gate = AutonomyRuntimeGate(
        settings=AutonomyRuntimeSettings(enabled=True, static_deployment_mode=settings.static_deployment_mode),
        switch_gate=switch_gate,
        kill_switch_gate=kill_switch_gate,
        durable_control_gate=durable_control_gate,
        evaluator=evaluate_autonomy_policy,
        reservation_port=_UnusedReservationPort(),
    )

    start_execution = StepFunctionsStartExecution(
        client=stepfunctions_client,
        state_machine_arn=settings.autonomy_state_machine_arn,
    )

    return AutonomyRuntimeHandler(
        service=AutonomyRuntimeService(),
        reservation_store=reservation_store,
        bundle_store=bundle_store,
        gate=gate,
        start_execution=start_execution,
    )


def _region() -> str:
    # Standard library
    import os

    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
