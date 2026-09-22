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

    def __post_init__(self) -> None:
        if self.mode not in _AUTHORITY_ORDER:
            raise ValueError(f"GBAW_OPERATIONS_MODE must be one of {OPERATIONS_MODES}")

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
