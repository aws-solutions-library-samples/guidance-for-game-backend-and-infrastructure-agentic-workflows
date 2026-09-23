"""Deployable Lambda evaluator entry/bootstrap for the E5 runtime (issue #439).

This module is the real, deployable composition point that turns strict,
server-owned environment settings into a live
:class:`~operations.autonomy_runtime.handler.AutonomyRuntimeHandler` and exposes
a **module-level Lambda ``handler``** that finishes the E5 source-level wiring.
Nothing here is "deferred to future infrastructure". It constructs, from env
only:

* the fresh, **separate** AppConfig autonomy switch
  (:class:`~operations.autonomy_switch.AutonomySwitchGate`) over the AppConfig
  Lambda-extension client, pointed at its own profile;
* the E4 cached kill-switch dispatch gate and the E4 **durable** control gate;
* the composite :class:`~operations.autonomy_gate.AutonomyRuntimeGate`;
* the durable :class:`~operations.autonomy_runtime.store.DynamoDbReservationStore`
  and the immutable :class:`~operations.autonomy_runtime.store.DynamoDbAutonomyBundleStore`;
* the code-owned :class:`DynamoDbAutonomyPolicyLoader`, the durable
  :class:`~operations.observation_store.DynamoDbObservationStore` observation
  loader, and the durable :class:`DynamoDbWindowStateLoader`;
* a Step Functions ``StartExecution`` client that starts the Standard workflow
  with ``operation_id`` **alone** under a **deterministic execution name**; and
* the :class:`AutonomyRuntimeHandler` and the :class:`AutonomyModuleRuntime` that
  tie a **closed** evaluation event to one durable, identifier-only dispatch.

The event contract is closed: it carries ONLY ``observation_operation_id`` and
the requested ``desired``/``minimum``/``maximum``. Identity, policy, limits,
authority, principal, and correlation are NEVER read from the event — they are
owned server-side (settings + the loaded policy). Unknown event fields are
refused.

Safety boundaries (ADR 0001 / AGENTS.md):

* **Default disabled + startup refusal.** :func:`resolve_autonomy_evaluator_settings`
  fails closed unless the static deployment mode is EXACTLY ``operate``, the
  executor capability maximum is EXACTLY ``operate``, and every autonomy
  identifier is configured. A default deployment provisions no autonomy control
  plane, so the bootstrap is never reached there.
* **The evaluator holds no provider-write credential.** It constructs ONLY
  DynamoDB, AppConfig, and Step Functions clients (it emits NO custom metric;
  rollback/health alarms use AWS-emitted metrics only) —
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
from datetime import datetime, timezone
from typing import Any, Callable

# Local modules
from operations.settings import resolve_executor_deployment_settings

# The autonomy-specific env keys, additive to the shared operations settings.
_AUTONOMY_STATE_MACHINE_KEY = "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN"
_AUTONOMY_SWITCH_PROFILE_KEY = "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE"
_APPCONFIG_APPLICATION_KEY = "GBAW_OPERATIONS_APPCONFIG_APPLICATION"
_APPCONFIG_ENVIRONMENT_KEY = "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT"
_APPCONFIG_PROFILE_KEY = "GBAW_OPERATIONS_APPCONFIG_PROFILE"
# The SEPARATE E5 autonomy AppConfig application/environment (09 stack). These
# are DISTINCT from the E4 kill-switch application/environment above so enabling
# E4 never enables autonomy and the evaluator can address BOTH switches.
_AUTONOMY_APPCONFIG_APPLICATION_KEY = "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION"
_AUTONOMY_APPCONFIG_ENVIRONMENT_KEY = "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT"
_APPCONFIG_EXTENSION_PORT_KEY = "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT"
_AUTONOMY_POLICY_ID_KEY = "GBAW_OPERATIONS_AUTONOMY_POLICY_ID"
_AUTONOMY_POLICY_VERSION_KEY = "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION"
_AUTONOMY_POLICY_HASH_KEY = "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH"
_AUTONOMY_STATE_ID_KEY = "GBAW_OPERATIONS_AUTONOMY_STATE_ID"
_AUTONOMY_SUBJECT_KEY = "GBAW_OPERATIONS_AUTONOMY_SUBJECT"
_AUTONOMY_CLIENT_KEY = "GBAW_OPERATIONS_AUTONOMY_CLIENT"

_REQUIRED_STATIC_MODE = "operate"
_STATE_MACHINE_ARN_PREFIX = "arn:aws:states:"

# The six ADR 0001 authority ceilings the runtime derives server-side. Each is
# the deployment's own authority; a model or request-body value can never
# contribute one. ``effective_authority`` is their minimum, so every ceiling
# below ``operate`` truthfully denies.
_AUTHORITY_INPUT_FIELDS = (
    "deployment_mode",
    "tenant_policy",
    "workspace_policy",
    "principal_authority",
    "capability_maximum",
    "operation_risk_policy",
)

# The closed evaluation-event contract. Exactly these keys, nothing else.
_EVENT_FIELDS = frozenset({"observation_operation_id", "desired", "minimum", "maximum"})


@dataclass(frozen=True, slots=True)
class AutonomyEvaluatorDeploymentSettings:
    """The strict, server-owned settings the evaluator bootstrap resolves from env.

    Embeds the frozen executor deployment settings (durable table, tenant/
    workspace, enrolled fleet id/ARN/location, capability maximum) and adds the
    autonomy-specific identifiers: the Standard Step Functions workflow ARN for
    autonomous execution, the SEPARATE AppConfig autonomy-switch profile, the E4
    kill-switch profile, the shared AppConfig application/environment/
    extension-port read target, the configured policy id/version/hash, the
    durable window-state id, and the trusted automation subject/client. Every
    field is validated at resolve time and fails closed.
    """

    executor: Any  # ExecutorDeploymentSettings
    static_deployment_mode: str
    autonomy_state_machine_arn: str
    autonomy_switch_profile: str
    kill_switch_profile: str
    autonomy_policy_id: str
    autonomy_policy_version: str
    autonomy_policy_hash: str
    autonomy_state_id: str
    automation_subject: str
    automation_client: str
    appconfig_application: str
    appconfig_environment: str
    # SEPARATE E5 autonomy AppConfig application/environment (09 stack). The
    # autonomy switch is read from THIS pair; the E4 kill switch is read from
    # ``appconfig_application``/``appconfig_environment`` above.
    autonomy_appconfig_application: str
    autonomy_appconfig_environment: str
    appconfig_extension_port: int

    def __post_init__(self) -> None:
        if self.static_deployment_mode != _REQUIRED_STATIC_MODE:
            raise ValueError("bounded autonomy requires the static deployment mode to be exactly 'operate'")
        # The executor capability maximum must ALSO be exactly ``operate``: an
        # autonomous write requires ``operate`` execution authority, so a lower
        # capability ceiling can never admit one and must refuse at startup.
        if getattr(self.executor, "capability_maximum", None) != _REQUIRED_STATIC_MODE:
            raise ValueError("bounded autonomy requires the executor capability maximum to be exactly 'operate'")
        for name in (
            "autonomy_state_machine_arn",
            "autonomy_switch_profile",
            "kill_switch_profile",
            "autonomy_policy_id",
            "autonomy_policy_version",
            "autonomy_policy_hash",
            "autonomy_state_id",
            "automation_subject",
            "automation_client",
            "appconfig_application",
            "appconfig_environment",
            "autonomy_appconfig_application",
            "autonomy_appconfig_environment",
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
        # The autonomy switch and the E4 kill switch must not resolve to the
        # SAME AppConfig configuration coordinate (application+environment+
        # profile); otherwise enabling one could read the other's document.
        if (
            self.autonomy_appconfig_application == self.appconfig_application
            and self.autonomy_appconfig_environment == self.appconfig_environment
            and self.autonomy_switch_profile == self.kill_switch_profile
        ):
            raise ValueError("the autonomy switch must not share the E4 kill-switch configuration coordinate")
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
    deployment mode is EXACTLY ``operate``, the executor capability maximum is
    EXACTLY ``operate``, and every autonomy identifier (including the durable
    state id and the trusted automation subject/client) is present. This is the
    single startup gate that keeps the default-disabled runtime from ever
    constructing a dispatchable handler in a misconfigured or non-operate
    deployment.
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
        autonomy_state_id=_required(source, _AUTONOMY_STATE_ID_KEY),
        automation_subject=_required(source, _AUTONOMY_SUBJECT_KEY),
        automation_client=_required(source, _AUTONOMY_CLIENT_KEY),
        appconfig_application=_required(source, _APPCONFIG_APPLICATION_KEY),
        appconfig_environment=_required(source, _APPCONFIG_ENVIRONMENT_KEY),
        autonomy_appconfig_application=_required(source, _AUTONOMY_APPCONFIG_APPLICATION_KEY),
        autonomy_appconfig_environment=_required(source, _AUTONOMY_APPCONFIG_ENVIRONMENT_KEY),
        appconfig_extension_port=extension_port,
    )


class StepFunctionsStartExecution:
    """Start the Standard autonomy workflow with an identifier-only input.

    The dispatch payload is serialized to the SFN ``input`` as ``operation_id``
    ALONE. No policy, limits, observation, window state, or credential crosses
    this boundary; the executor reloads the durable bundle by id and re-verifies
    every precondition itself before the single provider write.

    Idempotent replay
    -----------------
    Every start uses a **deterministic execution name** (the caller passes it —
    it is derived from the operation id). Step Functions rejects a second start
    of the same name with ``ExecutionAlreadyExists``; that is a *successful
    idempotent replay* (the workflow is already running/ran under that name), not
    a failure, so it is swallowed. Any OTHER start failure — a timeout, a reset
    connection, a throttle — is **ambiguous** (the start may or may not have
    taken) and is re-raised so the caller fails closed WITHOUT recording a
    confirmed dispatch. It never fabricates a "started" outcome.
    """

    __slots__ = ("_client", "_state_machine_arn")

    # The distinguished error code Step Functions returns for a duplicate name.
    _ALREADY_EXISTS = "ExecutionAlreadyExists"

    def __init__(self, *, client: Any, state_machine_arn: str) -> None:
        if not isinstance(state_machine_arn, str) or not state_machine_arn.startswith(_STATE_MACHINE_ARN_PREFIX):
            raise ValueError("state_machine_arn must be a Step Functions state machine ARN")
        self._client = client
        self._state_machine_arn = state_machine_arn

    def __call__(self, payload: Mapping[str, str], *, name: str) -> None:
        operation_id = payload["operation_id"]
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("execution name must be a non-empty string")
        try:
            # Serialize ONLY the operation id; nothing else crosses the boundary.
            self._client.start_execution(
                stateMachineArn=self._state_machine_arn,
                name=name,
                input=json.dumps({"operation_id": operation_id}, separators=(",", ":")),
            )
        except Exception as exc:  # noqa: BLE001 - classify duplicate vs ambiguous
            if _error_code(exc) == self._ALREADY_EXISTS:
                # A prior start under this deterministic name already took: an
                # idempotent replay is a success, not a failure.
                return
            # Ambiguous: re-raise so the caller fails closed without claiming a
            # confirmed dispatch. Never coerce this into a success.
            raise


def _error_code(exc: Exception) -> str | None:
    """Best-effort extraction of an AWS error code from a raised exception.

    Reads ``exc.response["Error"]["Code"]`` when present (botocore ClientError
    shape). Returns ``None`` when no structured code is available, so an
    unclassifiable failure is treated as ambiguous (re-raised), never as an
    idempotent duplicate.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


class AutonomyPolicyLoaderError(RuntimeError):
    """Loading or seeding the server-owned autonomy policy failed (fail closed).

    Raised when the configured policy is absent, when its stored hash does not
    match the configured hash, when the stored/seeded bytes do not satisfy the
    frozen #438 contract (including the ``policy_hash`` self-binding), or when a
    seed would overwrite a *different* immutable policy.
    """


class DynamoDbAutonomyPolicyLoader:
    """Durable, code-owned loader (and #440 seed API) for the autonomy policy.

    The policy is the server-owned authorization contract; it is NEVER supplied
    by the event or a model. It is stored as ONE canonical-JSON record on the
    existing single (issue #06) table at
    ``PK=AUTZPOLICY#<policy_id>#<policy_version>, SK=AUTZPOLICY``.

    * :meth:`load` fetches the exact policy by id/version and refuses unless the
      stored policy's ``policy_hash`` equals the CONFIGURED hash and the document
      re-validates against the frozen #438 contract. A mismatch fails closed.
    * :meth:`seed` (for the issue #440 deploy wrapper) persists a policy with a
      conditional immutable put after validating it against the frozen contract
      (which enforces ``policy_hash`` binds the canonical bytes). A second seed of
      an identical policy is idempotent; a differing one is refused without
      overwrite.

    Constructing the loader captures no credential and performs no I/O until a
    method is called. It performs no provider write and holds no GameLift or
    executor credential.
    """

    __slots__ = ("_client", "_table_name")

    _SK = "AUTZPOLICY"

    def __init__(self, *, client: Any, table_name: str) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name

    def seed(self, policy: Mapping[str, Any]) -> None:
        """Immutably persist a validated, hash-bound policy (deploy-time seed)."""
        # Local modules
        from operations.contracts.autonomy import AutonomyContractError, validate_autonomy_contract

        document = dict(policy)
        try:
            # Validates every field AND that ``policy_hash`` binds the canonical
            # bytes, so a policy whose declared hash disagrees is refused here.
            validate_autonomy_contract("gamelift-capacity-autonomy-policy", document)
        except AutonomyContractError as exc:
            raise AutonomyPolicyLoaderError("policy failed the frozen autonomy contract") from exc

        canonical = json.dumps(document, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        item = {
            "PK": {"S": self._policy_pk(document["policy_id"], document["policy_version"])},
            "SK": {"S": self._SK},
            "policy_id": {"S": str(document["policy_id"])},
            "policy_version": {"S": str(document["policy_version"])},
            "policy_hash": {"S": str(document["policy_hash"])},
            "policy": {"S": canonical},
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except Exception as exc:  # noqa: BLE001 - classify conditional vs unavailable
            if _is_conditional_failure(exc):
                existing = self._raw_policy(document["policy_id"], document["policy_version"])
                if existing is not None and existing == canonical:
                    return
                raise AutonomyPolicyLoaderError("a different policy is already sealed for this id/version") from exc
            raise AutonomyPolicyLoaderError("policy store is unavailable") from exc

    def load(self, *, policy_id: str, policy_version: str, policy_hash: str) -> dict[str, Any]:
        """Load the exact configured policy, failing closed on any drift."""
        # Local modules
        from operations.contracts.autonomy import AutonomyContractError, validate_autonomy_contract

        canonical = self._raw_policy(policy_id, policy_version)
        if canonical is None:
            raise AutonomyPolicyLoaderError("no policy is sealed for the configured id/version")
        try:
            document = json.loads(canonical)
        except (ValueError, TypeError) as exc:
            raise AutonomyPolicyLoaderError("stored policy is malformed") from exc
        if not isinstance(document, dict):
            raise AutonomyPolicyLoaderError("stored policy is malformed")
        # The stored policy must bind to the CONFIGURED id, version, AND hash. A
        # record whose declared id/version disagrees with the configured pins —
        # even if its self-hash is internally consistent — is a server-owned
        # config drift (or a mis-seeded/tampered record) and fails closed rather
        # than executing a policy the deployment did not pin. Checking the hash
        # alone is insufficient: the hash binds the bytes, not the deployment's
        # configured identity.
        if document.get("policy_id") != policy_id:
            raise AutonomyPolicyLoaderError("stored policy_id does not match the configured policy id")
        if document.get("policy_version") != policy_version:
            raise AutonomyPolicyLoaderError("stored policy_version does not match the configured policy version")
        if document.get("policy_hash") != policy_hash:
            raise AutonomyPolicyLoaderError("stored policy_hash does not match the configured policy hash")
        try:
            validate_autonomy_contract("gamelift-capacity-autonomy-policy", document)
        except AutonomyContractError as exc:
            raise AutonomyPolicyLoaderError("stored policy failed the frozen autonomy contract") from exc
        return document

    def _raw_policy(self, policy_id: str, policy_version: str) -> str | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": {"S": self._policy_pk(policy_id, policy_version)}, "SK": {"S": self._SK}},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, dict) else None
        if not isinstance(item, dict):
            return None
        cell = item.get("policy")
        if not isinstance(cell, dict) or "S" not in cell:
            raise AutonomyPolicyLoaderError("stored policy is malformed")
        return str(cell["S"])

    @staticmethod
    def _policy_pk(policy_id: str, policy_version: str) -> str:
        return f"AUTZPOLICY#{policy_id}#{policy_version}"


def _is_conditional_failure(exc: Exception) -> bool:
    code = _error_code(exc)
    return code in ("ConditionalCheckFailedException", "ConditionalCheckFailed")


class DynamoDbWindowStateLoader:
    """Load the current durable rolling-window state from the reservation store.

    Before reading the snapshot, the loader sweeps an EXPIRED active owner via the
    store's ``sweep_state`` (owner-index lookup, no ``Scan``). A crash between
    reserve and settle leaves a bounded, reclaimable lease rather than a wedged
    in-flight slot: the next evaluation reclaims the expired owner here, so
    ``current()`` reflects the released slot and a fresh reservation can proceed.
    A live lease is never reclaimed, and a sweep failure never blocks the read
    (the reserve fence still fails closed against a genuinely held slot).
    """

    __slots__ = ("_reservation_store", "_clock")

    def __init__(self, reservation_store: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self._reservation_store = reservation_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def load(self, state_id: str) -> dict[str, Any]:
        sweep = getattr(self._reservation_store, "sweep_state", None)
        if sweep is not None:
            try:
                sweep(state_id=state_id, now_epoch_seconds=int(self._clock().timestamp()))
            except Exception:  # noqa: BLE001 - a sweep failure never blocks the read; reserve still fences
                pass
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


class AutonomyModuleRuntime:
    """Turn a closed evaluation event into one durable, identifier-only dispatch.

    This is the piece the earlier commits deferred. It owns the trusted loaders
    and the server-side derivation of identity, policy, authority, principal, and
    correlation, and it wraps the pure
    :class:`~operations.autonomy_runtime.handler.AutonomyRuntimeHandler` with the
    truthful dispatch-audit bracket:

    1. Read the **closed** event (``observation_operation_id`` + requested triple
       only). Unknown fields or a missing requested field refuse.
    2. Load the succeeded canonical E1 observation from
       :class:`~operations.observation_store.DynamoDbObservationStore` under the
       server-owned workspace. A non-succeeded/absent observation refuses.
    3. Load the EXACT policy by the configured id/version/hash from the
       code-owned :class:`DynamoDbAutonomyPolicyLoader`.
    4. Load the configured durable window state.
    5. Derive authority (six server-owned ceilings), the trusted automation
       principal (from the policy), and a deterministic correlation envelope —
       none from the event.
    6. Record ``dispatch_requested`` BEFORE the pipeline's StartExecution, run
       the handler, and record ``dispatched`` only after a confirmed dispatch.
       After StartExecution uncertainty the ``dispatch_requested`` evidence is
       retained and no ``dispatched`` record is fabricated.
    """

    __slots__ = (
        "_settings",
        "_handler",
        "_observation_store",
        "_policy_loader",
        "_window_loader",
        "_clock",
    )

    def __init__(
        self,
        *,
        settings: AutonomyEvaluatorDeploymentSettings,
        handler: Any,
        observation_store: Any,
        policy_loader: Any,
        window_loader: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._observation_store = observation_store
        self._policy_loader = policy_loader
        self._window_loader = window_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate one closed event and return a small, closed result dict."""
        observation_operation_id, requested = self._read_closed_event(event)

        # Trusted observation: the succeeded canonical E1 observation owned by
        # the server-configured workspace. Nothing from the event contributes the
        # workspace or the observation content.
        workspace_id = self._settings.executor.observation.workspace_id
        observation = self._load_succeeded_observation(observation_operation_id, workspace_id)
        if observation is None:
            return {"outcome": "refused", "reason": "observation_not_succeeded"}

        # Trusted policy: the exact configured id/version/hash.
        policy = self._policy_loader.load(
            policy_id=self._settings.autonomy_policy_id,
            policy_version=self._settings.autonomy_policy_version,
            policy_hash=self._settings.autonomy_policy_hash,
        )

        # Durable window state by the configured state id.
        window_state = self._window_loader.load(self._settings.autonomy_state_id)

        evaluated_at = self._clock().astimezone(timezone.utc)

        inputs = self._build_inputs(
            policy=policy,
            observation=observation,
            window_state=window_state,
            requested=requested,
            observation_operation_id=observation_operation_id,
            evaluated_at=evaluated_at,
        )
        return self._dispatch_with_audit(inputs)

    # -- Event contract --------------------------------------------------

    @staticmethod
    def _read_closed_event(event: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
        if not isinstance(event, Mapping):
            raise ValueError("evaluation event must be a mapping")
        keys = set(event)
        if keys != _EVENT_FIELDS:
            unknown = sorted(keys - _EVENT_FIELDS)
            missing = sorted(_EVENT_FIELDS - keys)
            detail = []
            if unknown:
                detail.append("unknown fields: " + ", ".join(unknown))
            if missing:
                detail.append("missing fields: " + ", ".join(missing))
            raise ValueError("evaluation event contract violated (" + "; ".join(detail) + ")")
        observation_operation_id = event["observation_operation_id"]
        if not isinstance(observation_operation_id, str) or not observation_operation_id.strip():
            raise ValueError("observation_operation_id must be a non-empty string")
        requested: dict[str, int] = {}
        for field in ("desired", "minimum", "maximum"):
            value = event[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"requested {field} must be a non-negative integer")
            requested[field] = value
        return observation_operation_id, requested

    def _load_succeeded_observation(self, operation_id: str, workspace_id: str) -> dict[str, Any] | None:
        # Local modules
        from operations.observation import ObservationStatusView

        status = self._observation_store.load_status(operation_id=operation_id, workspace_id=workspace_id)
        if status is None or status.state is not ObservationStatusView.SUCCEEDED:
            return None
        observation = status.observation
        if not isinstance(observation, dict):
            return None
        return observation

    # -- Server-owned input assembly -------------------------------------

    def _build_inputs(
        self,
        *,
        policy: dict[str, Any],
        observation: dict[str, Any],
        window_state: dict[str, Any],
        requested: dict[str, int],
        observation_operation_id: str,
        evaluated_at: datetime,
    ) -> Any:
        # Local modules
        from operations.autonomy_runtime.service import AutonomyRuntimeInputs

        authority_inputs = self._derive_authority_inputs()
        automation_principal = {
            "source_type": "automation",
            "subject_id": self._settings.automation_subject,
            "client_id": self._settings.automation_client,
        }
        correlation = self._derive_correlation(observation_operation_id, requested, window_state)
        return AutonomyRuntimeInputs(
            policy=policy,
            observation=observation,
            authority_inputs=authority_inputs,
            automation_principal=automation_principal,
            window_state=window_state,
            requested=requested,
            correlation=correlation,
            evaluated_at=evaluated_at,
        )

    def _derive_authority_inputs(self) -> dict[str, str]:
        """Derive the six ADR 0001 authority ceilings from server-owned settings.

        Every ceiling is the deployment's static authority, capped by the
        executor capability maximum. Both are exactly ``operate`` by startup
        refusal, so the effective authority is ``operate``. No event/model value
        contributes any ceiling.
        """
        mode = self._settings.static_deployment_mode
        return {field: mode for field in _AUTHORITY_INPUT_FIELDS}

    @staticmethod
    def _derive_correlation(
        observation_operation_id: str, requested: Mapping[str, int], window_state: Mapping[str, Any]
    ) -> dict[str, str]:
        """Derive a deterministic, server-owned correlation envelope.

        Correlation is bound to the trusted observation + requested intent +
        window revision, never taken from the event. A retry of the same intent
        reproduces the same correlation ids.
        """
        # Standard library
        import hashlib

        fingerprint = "\u001f".join(
            [
                observation_operation_id,
                str(requested["desired"]),
                str(requested["minimum"]),
                str(requested["maximum"]),
                str(window_state.get("state_id", "")),
                str(window_state.get("state_revision", "")),
            ]
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return {"correlation_id": f"corr.{digest[:32]}", "request_id": f"req.{digest[32:64]}"}

    # -- Truthful dispatch audit -----------------------------------------

    def _dispatch_with_audit(self, inputs: Any) -> dict[str, Any]:
        """Bracket the handler dispatch with truthful requested/dispatched audit.

        A ``dispatch_requested`` record is written before the handler runs
        StartExecution; a ``dispatched`` record is written only after the handler
        reports a confirmed (or idempotently replayed) dispatch. If the handler
        refuses (including after an ambiguous StartExecution), the
        ``dispatch_requested`` evidence is retained and no ``dispatched`` record
        is fabricated.
        """
        # Local modules
        from operations.autonomy_runtime.handler import RuntimeDispatchOutcome
        from operations.contracts.execution import logical_action_id

        prepared = self._handler._service.prepare(inputs)
        if not prepared.authorized:
            # A denied decision never dispatches and never records a request.
            return {"outcome": "refused", "reason": "decision_denied"}

        operation_id = prepared.operation["operation_id"]
        execution_name = _execution_name(operation_id)
        bundle_store = self._handler._bundle_store

        # The dispatch audit is MANDATORY evidence, not best-effort. Record the
        # request BEFORE any StartExecution; if that durable write fails we must
        # NOT start an execution with no request evidence — refuse and release the
        # in-flight reservation so the slot is not wedged. (The reservation is
        # taken inside handler.dispatch, so at this point nothing is reserved yet;
        # a request-audit failure simply refuses before the handler runs.)
        try:
            _require_audit(bundle_store, "record_dispatch_requested", operation_id, execution_name)
        except Exception:  # noqa: BLE001 - a missing request audit refuses before any reserve/start
            return {"outcome": "refused", "reason": "dispatch_requested_audit_failed", "operation_id": operation_id}

        result = self._handler.dispatch(inputs)

        if result.outcome is not RuntimeDispatchOutcome.DISPATCHED:
            return {"outcome": "refused", "reason": result.reason, "operation_id": operation_id}

        # A confirmed/idempotent start MUST be durably recorded before we report a
        # proven dispatch. If the confirming audit fails, refuse and release the
        # in-flight reservation: an execution the runtime cannot prove it recorded
        # is not claimed as dispatched, and the executor's own reload requires the
        # exact dispatched audit before it verifies or writes.
        try:
            _require_audit(bundle_store, "record_dispatched", operation_id, execution_name)
        except Exception:  # noqa: BLE001 - an unrecorded start is not a proven dispatch
            action_id = logical_action_id(operation_id, prepared.operation["prepared_hash"])
            self._handler._release_in_flight(operation_id, action_id)
            return {"outcome": "refused", "reason": "dispatched_audit_failed", "operation_id": operation_id}

        return {"outcome": "dispatched", "operation_id": operation_id}


def _execution_name(operation_id: str) -> str:
    """Return the deterministic Step Functions execution name for an operation.

    The operation id is itself deterministic and unique per idempotent intent, so
    it is a stable execution name: a retry of the same intent starts the same
    named execution, and Step Functions collapses the duplicate to an idempotent
    ``ExecutionAlreadyExists``. Step Functions names are limited to 80 chars and
    exclude a small set of characters; the operation id (``op_`` + 26 lowercase
    base-36) satisfies both.
    """
    return operation_id[:80]


def _require_audit(bundle_store: Any, method_name: str, operation_id: str, execution_name: str) -> None:
    """Durably write a MANDATORY dispatch audit record; raise on any failure.

    The dispatch audit is fail-closed evidence, not a best-effort side note. A
    bundle store that cannot record the audit (missing method or a store failure)
    MUST NOT let a dispatch be treated as proven: the caller refuses and releases
    the reservation. An immutable conditional-refusal on a byte-identical record
    is already reconciled to success inside the store, so an idempotent replay
    does not raise here.
    """
    method = getattr(bundle_store, method_name, None)
    if method is None:
        raise RuntimeError(f"bundle store does not support {method_name}")
    method(operation_id=operation_id, execution_name=execution_name)


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
        application=settings.autonomy_appconfig_application,
        environment=settings.autonomy_appconfig_environment,
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


def build_module_runtime(
    *,
    settings: AutonomyEvaluatorDeploymentSettings,
    session: Any = None,
) -> AutonomyModuleRuntime:
    """Construct the full :class:`AutonomyModuleRuntime` from resolved settings.

    Wires the trusted loaders — the DynamoDB observation store, the code-owned
    policy loader, and the durable window-state loader — onto the runtime
    handler. Only DynamoDB, AppConfig, and Step Functions clients are created.
    """
    # Local modules
    from operations.autonomy_runtime.store import DynamoDbReservationStore
    from operations.observation_store import DynamoDbObservationStore

    if session is None:
        # Third-party packages
        import boto3
        from botocore.config import Config as BotocoreConfig

        config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
        session = boto3.Session(region_name=_region())
        dynamodb_client = session.client("dynamodb", config=config)
    else:
        dynamodb_client = session.client("dynamodb")

    table_name = settings.executor.observation.table_name

    handler = build_evaluator_handler(settings=settings, session=session)
    observation_store = DynamoDbObservationStore(client=dynamodb_client, table_name=table_name)
    policy_loader = DynamoDbAutonomyPolicyLoader(client=dynamodb_client, table_name=table_name)
    window_loader = DynamoDbWindowStateLoader(DynamoDbReservationStore(client=dynamodb_client, table_name=table_name))

    return AutonomyModuleRuntime(
        settings=settings,
        handler=handler,
        observation_store=observation_store,
        policy_loader=policy_loader,
        window_loader=window_loader,
    )


# -- Module-level Lambda handler --------------------------------------------
#
# The cached production runtime. It is built lazily on the first invocation from
# the process environment and reused across warm invocations. Import performs no
# I/O and reads no environment; the runtime is constructed only when the handler
# is first called, so a misconfigured deployment fails closed at first invoke.
_RUNTIME: AutonomyModuleRuntime | None = None


def _production_runtime() -> AutonomyModuleRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = build_module_runtime(settings=resolve_autonomy_evaluator_settings())
    return _RUNTIME


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Module-level Lambda entrypoint: evaluate one closed event and dispatch.

    The cached production runtime turns the closed event
    (``observation_operation_id`` + requested desired/min/max) into one durable,
    identifier-only dispatch, using only trusted server-owned loaders. Returns a
    small closed result dict (``outcome`` plus, on success, ``operation_id``).
    """
    return _production_runtime().evaluate(event)


def _region() -> str:
    # Standard library
    import os

    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
