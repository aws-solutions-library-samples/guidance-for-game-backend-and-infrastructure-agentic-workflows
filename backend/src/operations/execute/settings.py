"""Frozen environment contract for the OPTIONAL E3 execution plane (issue #415).

Resolved once and validated, failing closed. The runtime authority
``GBAW_OPERATIONS_EXECUTION_MODE`` mirrors the CloudFormation ``ExecutionMode``
parameter: only ``remediate`` is an enabled mode; anything else (default
``disabled``) makes the executor and dispatcher fail closed.
"""

from __future__ import annotations

# Standard library
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# The only enabled runtime authority for the execution plane. Any other value
# (including the default ``disabled``) is fail-closed.
EXECUTION_MODE_REMEDIATE = "remediate"
EXECUTION_MODE_DISABLED = "disabled"
EXECUTION_MODES = frozenset({EXECUTION_MODE_DISABLED, EXECUTION_MODE_REMEDIATE})

_FLEET_ID_PATTERN = re.compile(r"^fleet-[a-f0-9-]{1,120}$")


@dataclass(frozen=True)
class ExecutionDeploymentSettings:
    """The frozen, server-owned execution deployment contract."""

    mode: str
    table_name: str
    metric_namespace: str
    enrolled_fleet_id: str
    state_machine_arn: str

    @property
    def enabled(self) -> bool:
        """True only when the runtime kill switch authorizes remediation."""
        return self.mode == EXECUTION_MODE_REMEDIATE


def _required_str(source: Mapping[str, str], key: str) -> str:
    value = (source.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_str(source: Mapping[str, str], key: str) -> str:
    return (source.get(key) or "").strip()


def resolve_execution_settings(env: Mapping[str, str] | None = None) -> ExecutionDeploymentSettings:
    """Resolve the execution deployment contract, failing closed.

    The mode defaults to ``disabled`` (fail-closed) when unset or unknown. The
    table name, metric namespace, and — for the executor — the enrolled fleet id
    are required so a mis-provisioned deployment fails at resolution rather than
    silently acting.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw_mode = (source.get("GBAW_OPERATIONS_EXECUTION_MODE") or EXECUTION_MODE_DISABLED).strip()
    mode = raw_mode if raw_mode in EXECUTION_MODES else EXECUTION_MODE_DISABLED

    fleet_id = _optional_str(source, "GBAW_OPERATIONS_ENROLLED_FLEET_ID")
    if fleet_id and not _FLEET_ID_PATTERN.fullmatch(fleet_id):
        raise ValueError("GBAW_OPERATIONS_ENROLLED_FLEET_ID is not a valid fleet id")

    return ExecutionDeploymentSettings(
        mode=mode,
        table_name=_required_str(source, "GBAW_OPERATIONS_TABLE_NAME"),
        metric_namespace=_required_str(source, "GBAW_OPERATIONS_METRIC_NAMESPACE"),
        enrolled_fleet_id=fleet_id,
        state_machine_arn=_optional_str(source, "GBAW_OPERATIONS_STATE_MACHINE_ARN"),
    )
