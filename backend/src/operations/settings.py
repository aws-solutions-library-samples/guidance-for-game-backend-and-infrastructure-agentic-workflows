"""Centralized, fail-closed operations deployment settings (ADR 0001).

The single backend-enforced deployment ceiling is ``GBAW_OPERATIONS_MODE``
(``disabled|observe|advise|remediate|operate``), defaulting to ``disabled`` so an
existing deployment stays read-only until an owner explicitly enables operations.
This module resolves that ceiling and the observe-phase read budgets from the
environment with the existing centralized configuration pattern, and never reads
provider write settings — the observe phase has no write path.

Values are validated at load time and any invalid value fails closed (raises)
rather than silently downgrading, so a misconfigured deployment cannot present a
higher authority than intended.
"""

from __future__ import annotations

# Standard library
import os
from collections.abc import Mapping
from dataclasses import dataclass

# ADR 0001 authority lattice; lower is more restrictive.
OPERATIONS_MODES = ("disabled", "observe", "advise", "remediate", "operate")
_AUTHORITY_ORDER = {mode: index for index, mode in enumerate(OPERATIONS_MODES)}

_DEFAULT_MODE = "disabled"
_OBSERVE_MODE = "observe"

# E0-derived observe-phase budgets (ADR 0005 / issue #412). Three reads at 3.0s,
# 3.0s persistence + canonical serialization, 3.0s cancellation margin => 15.0s
# total request deadline, well under the 30s API Gateway integration ceiling.
DEFAULT_PER_READ_BUDGET_S = 3.0
DEFAULT_PERSISTENCE_BUDGET_S = 3.0
DEFAULT_CANCELLATION_MARGIN_S = 3.0

# Observation freshness / DynamoDB TTL horizon for the transient state.
DEFAULT_OBSERVATION_TTL_S = 1800


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


@dataclass(frozen=True, slots=True)
class OperationsSettings:
    """Resolved, validated operations deployment ceiling and observe budgets."""

    mode: str
    per_read_budget_s: float
    persistence_budget_s: float
    cancellation_margin_s: float
    observation_ttl_s: int

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

    def authority_ceiling(self, *, requested: str) -> str:
        """Return the lower of the deployment ceiling and a requested authority."""
        if requested not in _AUTHORITY_ORDER:
            raise ValueError(f"requested authority must be one of {OPERATIONS_MODES}")
        return min((self.mode, requested), key=_AUTHORITY_ORDER.__getitem__)


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
    )
