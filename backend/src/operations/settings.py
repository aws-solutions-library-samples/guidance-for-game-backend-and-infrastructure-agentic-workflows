"""Centralized, fail-closed operations deployment settings (ADR 0001).

The single backend-enforced deployment ceiling is ``GBAW_OPERATIONS_MODE``
(``disabled|observe|advise|remediate|operate``), defaulting to ``disabled`` so an
existing deployment stays read-only until an owner explicitly enables operations.
This module resolves that ceiling and the observe-phase read budgets from the
environment with the existing centralized configuration pattern, and never reads
provider write settings — the observe phase has no write path.

The full deployment contract needed to bootstrap the deployable observe handler
is frozen here so a misconfigured deployment fails closed at load rather than at
request time. In addition to the ceiling and budgets it resolves the DynamoDB
table name, the CloudWatch metric namespace, and the trusted tenant/workspace/
audience binding the handler uses to construct verified principals.

Values are validated at load time and any invalid value fails closed (raises)
rather than silently downgrading, so a misconfigured deployment cannot present a
higher authority than intended.
"""

from __future__ import annotations

# Standard library
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ADR 0001 authority lattice; lower is more restrictive.
OPERATIONS_MODES = ("disabled", "observe", "advise", "remediate", "operate")
_AUTHORITY_ORDER = {mode: index for index, mode in enumerate(OPERATIONS_MODES)}

_DEFAULT_MODE = "disabled"
_OBSERVE_MODE = "observe"
_ADVISE_MODE = "advise"

# E0-derived observe-phase budgets (ADR 0005 / issue #412). Three reads at 3.0s,
# 3.0s persistence + canonical serialization, 3.0s cancellation margin => 15.0s
# total request deadline, well under the 30s API Gateway integration ceiling.
DEFAULT_PER_READ_BUDGET_S = 3.0
DEFAULT_PERSISTENCE_BUDGET_S = 3.0
DEFAULT_CANCELLATION_MARGIN_S = 3.0

# Observation freshness / DynamoDB TTL horizon for the transient state.
DEFAULT_OBSERVATION_TTL_S = 1800

# E2 prepare/approval lifecycle windows (issue #414). A prepared operation
# awaiting approval lives for at most PREPARATION_EXPIRY_S; a granted approval
# is valid for at most APPROVAL_EXPIRY_S. Both are bounded by the trusted E1
# observation revision's own expiry at prepare/approve time.
DEFAULT_PREPARATION_EXPIRY_S = 900
DEFAULT_APPROVAL_EXPIRY_S = 1800

# Self-approval (the requester approving their own operation) is denied by
# default; an owner opts in explicitly and it still only ever admits an
# explicit low-risk capability. Fails closed.
DEFAULT_LOW_RISK_SELF_APPROVAL = False

# The server-owned Cognito group a direct E2 approver must belong to. This is a
# *group*, not a scope: a real access token's ``scope`` claim is not the Cognito
# app client id, so approver authority is bound to a group the identity provider
# controls (``admin``), never to the trusted app client id. A ``users`` member
# cannot approve; only an ``admin`` member can.
DEFAULT_APPROVER_GROUP = "admin"

# Server-owned, fail-closed capacity bounds (issue #414). The default admits
# only a fully clamped [0, 1] fleet with a single-instance step, so a deployment
# that forgets to configure real bounds cannot authorize a large capacity swing.
# These are always at or inside the code-owned playbook parameter envelope.
DEFAULT_CAPACITY_FLOOR = 0
DEFAULT_CAPACITY_CEILING = 1
DEFAULT_CAPACITY_MAX_STEP = 1

# The DynamoDB TTL attribute name is frozen: the table's TimeToLiveSpecification
# and every written item agree on ``ttl``.
TTL_ATTRIBUTE = "ttl"

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive number of seconds") from exc
    if not (value > 0):
        raise ValueError(f"{key} must be a positive number of seconds")
    return value


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _int_field(env: Mapping[str, str], key: str, default: int, *, minimum: int) -> int:
    """Parse a strict integer no less than ``minimum``, failing closed.

    Rejects non-integer text (including floats like ``1.5``) so a malformed
    bound never silently rounds or defaults to a permissive value.
    """
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    token = raw.strip()
    try:
        value = int(token)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer no less than {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{key} must be an integer no less than {minimum}")
    return value


_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    """Parse a strict, fail-closed boolean opt-in from the environment."""
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"{key} must be a boolean (true/false)")


def _required_str(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    if raw is None or not raw.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return raw.strip()


def _required_identifier(env: Mapping[str, str], key: str) -> str:
    value = _required_str(env, key)
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{key} must be a valid operations identifier")
    return value


def _optional_str(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


@dataclass(frozen=True, slots=True)
class OperationsSettings:
    """Resolved, validated operations deployment ceiling and observe budgets."""

    mode: str
    per_read_budget_s: float
    persistence_budget_s: float
    cancellation_margin_s: float
    observation_ttl_s: int
    preparation_expiry_s: int = DEFAULT_PREPARATION_EXPIRY_S
    approval_expiry_s: int = DEFAULT_APPROVAL_EXPIRY_S
    low_risk_self_approval_enabled: bool = DEFAULT_LOW_RISK_SELF_APPROVAL
    approver_group: str = DEFAULT_APPROVER_GROUP
    capacity_floor: int = DEFAULT_CAPACITY_FLOOR
    capacity_ceiling: int = DEFAULT_CAPACITY_CEILING
    capacity_max_step: int = DEFAULT_CAPACITY_MAX_STEP

    def __post_init__(self) -> None:
        if self.mode not in _AUTHORITY_ORDER:
            raise ValueError(f"GBAW_OPERATIONS_MODE must be one of {OPERATIONS_MODES}")
        if not isinstance(self.approver_group, str) or not self.approver_group.strip():
            raise ValueError("approver_group must be a non-empty string")
        self._validate_capacity_bounds()

    def _validate_capacity_bounds(self) -> None:
        floor, ceiling, max_step = self.capacity_floor, self.capacity_ceiling, self.capacity_max_step
        for value, name in ((floor, "capacity_floor"), (ceiling, "capacity_ceiling"), (max_step, "capacity_max_step")):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
        if floor < 0:
            raise ValueError("capacity_floor must be non-negative")
        if ceiling < floor:
            raise ValueError("capacity_ceiling must be no less than capacity_floor")
        if max_step <= 0:
            raise ValueError("capacity_max_step must be a positive integer")
        # A single step can never exceed the reachable span of the bounds; a
        # larger max_step would be meaningless and could mask a misconfiguration.
        # A fully clamped floor==ceiling deployment still allows a step of 1 so
        # the safe default (0/1/1 and 0/0/1) validates.
        if max_step > max(1, ceiling - floor):
            raise ValueError("capacity_max_step must not exceed the bound span")

    @property
    def operations_enabled(self) -> bool:
        """Whether any operations authority above ``disabled`` is configured."""
        return self.mode != _DEFAULT_MODE

    @property
    def observe_enabled(self) -> bool:
        """Whether the deployment ceiling admits the read-only observe phase."""
        return _AUTHORITY_ORDER[self.mode] >= _AUTHORITY_ORDER[_OBSERVE_MODE]

    @property
    def advise_enabled(self) -> bool:
        """Whether the deployment ceiling admits the deterministic advise phase."""
        return _AUTHORITY_ORDER[self.mode] >= _AUTHORITY_ORDER[_ADVISE_MODE]

    @property
    def e2_enabled(self) -> bool:
        """Whether the deployment ceiling admits the E2 prepare/approval surface.

        The E2 prepare/approval lifecycle (issue #414) is gated by the same
        ``advise`` ceiling as the deterministic advise phase: ``advise`` and
        every higher mode enable E1 observe *and* E2, while ``disabled`` and a
        bare ``observe`` ceiling deny it. E2 performs no provider write; the
        higher ``remediate``/``operate`` ceilings gate later phases, not E2.
        """
        return _AUTHORITY_ORDER[self.mode] >= _AUTHORITY_ORDER[_ADVISE_MODE]

    def authority_ceiling(self, *, requested: str) -> str:
        """Return the lower of the deployment ceiling and a requested authority."""
        if requested not in _AUTHORITY_ORDER:
            raise ValueError(f"requested authority must be one of {OPERATIONS_MODES}")
        return min((self.mode, requested), key=_AUTHORITY_ORDER.__getitem__)


@dataclass(frozen=True, slots=True)
class ObservationDeploymentSettings:
    """The full frozen deployment contract the observe handler bootstraps from.

    All fields are resolved and validated at load time. It embeds the authority
    ceiling/budget :class:`OperationsSettings` and adds the durable-store,
    metrics, and trusted-identity binding a deployable Lambda needs. A missing or
    malformed value fails closed (raises) rather than defaulting to a permissive
    or unbound value.
    """

    operations: OperationsSettings
    table_name: str
    metric_namespace: str
    tenant_id: str
    workspace_id: str
    trusted_audience: str

    @property
    def mode(self) -> str:
        return self.operations.mode

    @property
    def observe_enabled(self) -> bool:
        return self.operations.observe_enabled

    @property
    def e2_enabled(self) -> bool:
        return self.operations.e2_enabled


def resolve_operations_settings(env: Mapping[str, str] | None = None) -> OperationsSettings:
    """Resolve operations settings from the environment, failing closed."""
    source: Mapping[str, str] = os.environ if env is None else env
    mode = (source.get("GBAW_OPERATIONS_MODE") or _DEFAULT_MODE).strip() or _DEFAULT_MODE
    return OperationsSettings(
        mode=mode,
        per_read_budget_s=_positive_float(source, "GBAW_OPERATIONS_PER_READ_BUDGET_S", DEFAULT_PER_READ_BUDGET_S),
        persistence_budget_s=_positive_float(
            source, "GBAW_OPERATIONS_PERSISTENCE_BUDGET_S", DEFAULT_PERSISTENCE_BUDGET_S
        ),
        cancellation_margin_s=_positive_float(
            source, "GBAW_OPERATIONS_CANCELLATION_MARGIN_S", DEFAULT_CANCELLATION_MARGIN_S
        ),
        observation_ttl_s=_positive_int(source, "GBAW_OPERATIONS_OBSERVATION_TTL_S", DEFAULT_OBSERVATION_TTL_S),
        preparation_expiry_s=_positive_int(
            source, "GBAW_OPERATIONS_PREPARATION_EXPIRY_S", DEFAULT_PREPARATION_EXPIRY_S
        ),
        approval_expiry_s=_positive_int(source, "GBAW_OPERATIONS_APPROVAL_EXPIRY_S", DEFAULT_APPROVAL_EXPIRY_S),
        low_risk_self_approval_enabled=_bool(
            source, "GBAW_OPERATIONS_LOW_RISK_SELF_APPROVAL", DEFAULT_LOW_RISK_SELF_APPROVAL
        ),
        approver_group=_optional_str(source, "GBAW_OPERATIONS_APPROVER_GROUP", DEFAULT_APPROVER_GROUP),
        capacity_floor=_int_field(source, "GBAW_OPERATIONS_CAPACITY_FLOOR", DEFAULT_CAPACITY_FLOOR, minimum=0),
        capacity_ceiling=_int_field(source, "GBAW_OPERATIONS_CAPACITY_CEILING", DEFAULT_CAPACITY_CEILING, minimum=0),
        capacity_max_step=_int_field(source, "GBAW_OPERATIONS_CAPACITY_MAX_STEP", DEFAULT_CAPACITY_MAX_STEP, minimum=1),
    )


def resolve_observation_deployment_settings(
    env: Mapping[str, str] | None = None,
) -> ObservationDeploymentSettings:
    """Resolve the full frozen observe deployment contract, failing closed."""
    source: Mapping[str, str] = os.environ if env is None else env
    return ObservationDeploymentSettings(
        operations=resolve_operations_settings(source),
        table_name=_required_str(source, "GBAW_OPERATIONS_TABLE_NAME"),
        metric_namespace=_required_str(source, "GBAW_OPERATIONS_METRIC_NAMESPACE"),
        tenant_id=_required_identifier(source, "GBAW_OPERATIONS_TENANT_ID"),
        workspace_id=_required_identifier(source, "GBAW_OPERATIONS_WORKSPACE_ID"),
        trusted_audience=_required_str(source, "GBAW_OPERATIONS_TRUSTED_AUDIENCE"),
    )


# -- E3 execute-phase deployment settings (issue #415) ------------------------
#
# The executor must run at or above ``remediate`` (both the deployment mode and
# the capability maximum) before it may issue a provider write. The default
# ``capability_maximum`` is ``remediate``: the executor never grants itself
# ``operate``. The enrolled fleet id/ARN/location are the server-owned binding
# the executor re-verifies the operation target against; the ARN is never taken
# from the operation. The state machine ARN is the Standard workflow the
# dispatcher starts with only ``{operation_id}``.
_EXECUTE_MODE = "remediate"


@dataclass(frozen=True, slots=True)
class ExecutorDeploymentSettings:
    """The full frozen E3 deployment contract the execute Lambdas bootstrap from.

    It embeds the observe deployment settings (table, metrics, tenant/workspace,
    trusted audience, authority ceiling) and adds the E3-specific bindings. Every
    field is resolved and validated at load time and fails closed.
    """

    observation: ObservationDeploymentSettings
    state_machine_arn: str
    capability_maximum: str
    enrolled_fleet_id: str
    enrolled_fleet_arn: str
    enrolled_location: str
    admin_group: str

    def __post_init__(self) -> None:
        if self.capability_maximum not in _AUTHORITY_ORDER:
            raise ValueError(f"capability_maximum must be one of {OPERATIONS_MODES}")
        for name in (
            "state_machine_arn",
            "enrolled_fleet_id",
            "enrolled_fleet_arn",
            "enrolled_location",
            "admin_group",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not self.enrolled_fleet_arn.startswith("arn:aws:gamelift:"):
            raise ValueError("enrolled_fleet_arn must be a GameLift fleet ARN")

    @property
    def execute_enabled(self) -> bool:
        """Whether the deployment ceiling AND capability admit a provider write."""
        return (
            _AUTHORITY_ORDER[self.observation.mode] >= _AUTHORITY_ORDER[_EXECUTE_MODE]
            and _AUTHORITY_ORDER[self.capability_maximum] >= _AUTHORITY_ORDER[_EXECUTE_MODE]
        )


def resolve_executor_deployment_settings(
    env: Mapping[str, str] | None = None,
) -> ExecutorDeploymentSettings:
    """Resolve the full frozen E3 deployment contract, failing closed."""
    source: Mapping[str, str] = os.environ if env is None else env
    return ExecutorDeploymentSettings(
        observation=resolve_observation_deployment_settings(source),
        state_machine_arn=_required_str(source, "GBAW_OPERATIONS_STATE_MACHINE_ARN"),
        capability_maximum=_optional_str(source, "GBAW_OPERATIONS_CAPABILITY_MAXIMUM", _EXECUTE_MODE),
        enrolled_fleet_id=_required_str(source, "GBAW_OPERATIONS_ENROLLED_FLEET_ID"),
        enrolled_fleet_arn=_required_str(source, "GBAW_OPERATIONS_ENROLLED_FLEET_ARN"),
        enrolled_location=_required_str(source, "GBAW_OPERATIONS_ENROLLED_LOCATION"),
        admin_group=_optional_str(source, "GBAW_OPERATIONS_APPROVER_GROUP", DEFAULT_APPROVER_GROUP),
    )


# -- E4 control-plane deployment settings (issue #416) ------------------------
#
# The E4 control plane reads the kill-switch on the request path through the AWS
# AppConfig Lambda extension (localhost endpoint) and publishes changes back to
# AppConfig. These settings resolve the extension read target, the publisher
# write target and its two deployment strategies, the admin group, the HMAC
# cursor signing key, and whether the capability is provisioned. Every field is
# resolved and validated at load time and fails closed.


@dataclass(frozen=True, slots=True)
class ControlPlaneDeploymentSettings:
    """The full frozen E4 control-plane deployment contract, fail-closed."""

    observation: ObservationDeploymentSettings
    admin_group: str
    appconfig_application: str
    appconfig_environment: str
    appconfig_profile: str
    appconfig_extension_port: int
    appconfig_gradual_strategy_id: str
    appconfig_immediate_strategy_id: str
    provisioned: bool
    # Exactly one cursor-key source must be configured. In production the HMAC
    # signing key is a Secrets Manager secret referenced by ARN and resolved at
    # bootstrap; the raw key is a local-test-only convenience. Never both.
    cursor_signing_key: str | None = None
    cursor_signing_key_secret_arn: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "admin_group",
            "appconfig_application",
            "appconfig_environment",
            "appconfig_profile",
            "appconfig_gradual_strategy_id",
            "appconfig_immediate_strategy_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not (1 <= self.appconfig_extension_port <= 65535):
            raise ValueError("appconfig_extension_port must be a valid TCP port")
        # Exactly one cursor-key source (raw local-test key XOR secret ARN).
        raw = self.cursor_signing_key
        arn = self.cursor_signing_key_secret_arn
        has_raw = isinstance(raw, str) and bool(raw.strip())
        has_arn = isinstance(arn, str) and bool(arn.strip())
        if has_raw and has_arn:
            raise ValueError(
                "configure exactly one of GBAW_OPERATIONS_CURSOR_SIGNING_KEY (local test) "
                "or GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN (production), not both"
            )
        if not has_raw and not has_arn:
            raise ValueError(
                "configure a cursor signing key source: "
                "GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN (production) or "
                "GBAW_OPERATIONS_CURSOR_SIGNING_KEY (local test)"
            )
        # A short raw signing key cannot provide meaningful HMAC strength.
        if has_raw and len(raw) < 16:  # type: ignore[arg-type]
            raise ValueError("cursor_signing_key must be at least 16 characters")
        if has_arn and not arn.startswith("arn:aws:secretsmanager:"):  # type: ignore[union-attr]
            raise ValueError("cursor_signing_key_secret_arn must be a Secrets Manager ARN")

    @property
    def mode(self) -> str:
        return self.observation.mode


def resolve_control_plane_deployment_settings(
    env: Mapping[str, str] | None = None,
) -> ControlPlaneDeploymentSettings:
    """Resolve the full frozen E4 control-plane deployment contract, fail-closed."""
    source: Mapping[str, str] = os.environ if env is None else env
    return ControlPlaneDeploymentSettings(
        observation=resolve_observation_deployment_settings(source),
        admin_group=_optional_str(source, "GBAW_OPERATIONS_APPROVER_GROUP", DEFAULT_APPROVER_GROUP),
        appconfig_application=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_APPLICATION"),
        appconfig_environment=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT"),
        appconfig_profile=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_PROFILE"),
        appconfig_extension_port=_positive_int(source, "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT", 2772),
        appconfig_gradual_strategy_id=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_GRADUAL_STRATEGY_ID"),
        appconfig_immediate_strategy_id=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_IMMEDIATE_STRATEGY_ID"),
        cursor_signing_key=_optional_or_none(source, "GBAW_OPERATIONS_CURSOR_SIGNING_KEY"),
        cursor_signing_key_secret_arn=_optional_or_none(source, "GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN"),
        provisioned=_bool(source, "GBAW_OPERATIONS_CONTROL_PROVISIONED", True),
    )


def _optional_or_none(env: Mapping[str, str], key: str) -> str | None:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def load_cursor_signing_key(
    settings: "ControlPlaneDeploymentSettings",
    *,
    secretsmanager_client: Any,
) -> str:
    """Resolve the cursor HMAC signing key, failing closed.

    In production the key is stored in AWS Secrets Manager and referenced by ARN;
    it is read once at bootstrap from the exact secret id and never logged. The
    raw ``GBAW_OPERATIONS_CURSOR_SIGNING_KEY`` is a local-test-only convenience.
    A missing/short/unreadable secret fails closed (raises); the ARN path
    requires a Secrets Manager client.
    """
    raw = settings.cursor_signing_key
    if isinstance(raw, str) and raw.strip():
        return raw
    arn = settings.cursor_signing_key_secret_arn
    if not (isinstance(arn, str) and arn.strip()):
        raise ValueError("no cursor signing key source is configured")
    if secretsmanager_client is None:
        raise ValueError("a Secrets Manager client is required to read the cursor signing key secret")
    response = secretsmanager_client.get_secret_value(SecretId=arn)
    secret = response.get("SecretString") if isinstance(response, Mapping) else None
    if not isinstance(secret, str) or len(secret) < 16:
        # Never log or echo the secret value; only its adequacy is reported.
        raise ValueError("cursor signing key secret is missing or too short")
    return secret


# -- E4 kill-switch extension bootstrap (issue #416) --------------------------
#
# The three deployable write-capable entrypoints (E2 prepare in the observe
# Lambda, the E3 dispatcher, the E3 executor) enforce the deployment-wide
# kill-switch by constructing a real KillSwitchGate over the AppConfig Lambda
# extension. That gate is built only when the AppConfig read target is
# configured. To keep a pre-E4 deployment backward compatible, ALL three core
# identifiers (application/environment/profile) absent means "no gate" (a no-op
# gate that never denies). Any *partial* configuration fails closed at startup:
# a half-configured switch must never silently leave the write path ungated.

CONTROL_MODES = ("disabled", "enabled")
_DEFAULT_CONTROL_MODE = "disabled"

_KILL_SWITCH_CORE_KEYS = (
    "GBAW_OPERATIONS_APPCONFIG_APPLICATION",
    "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT",
    "GBAW_OPERATIONS_APPCONFIG_PROFILE",
)


@dataclass(frozen=True, slots=True)
class KillSwitchExtensionSettings:
    """Resolved AppConfig Lambda extension read target for the kill-switch."""

    application: str
    environment: str
    profile: str
    extension_port: int

    def __post_init__(self) -> None:
        for name in ("application", "environment", "profile"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not (1 <= self.extension_port <= 65535):
            raise ValueError("extension_port must be a valid TCP port")


def resolve_kill_switch_extension_settings(
    env: Mapping[str, str] | None = None,
) -> "KillSwitchExtensionSettings | None":
    """Resolve the optional AppConfig extension read target, failing closed.

    Returns ``None`` when all three core AppConfig identifiers are absent (a
    pre-E4 deployment); returns a validated settings object when they are all
    present; and raises on any partial configuration.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    present = [key for key in _KILL_SWITCH_CORE_KEYS if (source.get(key) or "").strip()]
    if not present:
        return None
    if len(present) != len(_KILL_SWITCH_CORE_KEYS):
        missing = [key for key in _KILL_SWITCH_CORE_KEYS if key not in present]
        raise ValueError("partial AppConfig kill-switch configuration; missing " + ", ".join(sorted(missing)))
    return KillSwitchExtensionSettings(
        application=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_APPLICATION"),
        environment=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT"),
        profile=_required_str(source, "GBAW_OPERATIONS_APPCONFIG_PROFILE"),
        extension_port=_positive_int(source, "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT", 2772),
    )


def resolve_control_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve the dedicated control-plane mode (``enabled``/``disabled``).

    The control mode is deliberately independent of ``GBAW_OPERATIONS_MODE`` so
    admin controls (list/detail/kill-switch read + the CAS control write) remain
    available to *recover* operations even while static execution is disabled.
    Defaults to ``disabled`` and fails closed on any unrecognized value.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get("GBAW_OPERATIONS_CONTROL_MODE")
    if raw is None or not raw.strip():
        return _DEFAULT_CONTROL_MODE
    token = raw.strip().lower()
    if token not in CONTROL_MODES:
        raise ValueError(f"GBAW_OPERATIONS_CONTROL_MODE must be one of {CONTROL_MODES}")
    return token
