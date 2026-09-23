"""Code-owned, immutable GameLift capacity bounded-autonomy playbook (issue #438).

The E5 autonomous prepared operation and its policy bind a ``playbook_hash`` — a
stable, deterministic digest of the *complete* trusted bounded-autonomy playbook
and every transitive v2, E1 observation, and shared-common schema hash that a
future autonomous executor would honor. This is a **v2** playbook
(``gamelift.capacity-adjustment/2.0``): it is deliberately distinct from the E2
``playbook.gamelift-capacity`` / ``1.0.0`` definition and produces a **distinct
hash**, so a v1 human-approved operation and a v2 autonomous operation can never
share a playbook binding.

The executor identity/binding is intentionally **preserved** from E2/E3
(``executor.gamelift-capacity`` / ``1.0``): the single ``UpdateFleetCapacity``
write machinery is reused unchanged. What differs is the *precondition set* — the
autonomous playbook requires the deterministic guardrail envelope
(bounds/risk/freshness/budget/cooldown/frequency/concurrency/anti-oscillation/
decision-expiry) instead of a granted human approval, and its required execution
authority is ``operate`` — so its hash is distinct even though the executor id is
the same.

Nothing in this module reads the environment, holds a credential, or performs
I/O. The definition here is the single source of truth for the v2 playbook hash.
"""

from __future__ import annotations

# Standard library
from types import MappingProxyType
from typing import Any, Mapping

# Local modules
from operations.contracts.autonomy import (
    ACTION,
    AUTONOMY_SCHEMA_NAMES,
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    PROFILE,
    REQUIRED_EXECUTION_AUTHORITY,
    load_autonomy_schema,
)
from operations.contracts.canonical import canonical_sha256
from operations.contracts.observation import load_observation_schema
from operations.contracts.validation import load_schema

PLAYBOOK_ID = "playbook.gamelift-capacity-autonomy"
PLAYBOOK_VERSION = "2.0.0"

# The executor identity/binding is preserved from E2/E3: the same executor runs
# the single UpdateFleetCapacity write. The autonomy path differs in its
# precondition set and execution authority, not its executor.
EXECUTOR_ID = "executor.gamelift-capacity"
EXECUTOR_BINDING_VERSION = "1.0"

# The retry policy the future autonomous executor honors. Frozen so a change is a
# deliberate, hash-visible playbook revision.
RETRY_POLICY: Mapping[str, Any] = MappingProxyType(
    {
        "max_attempts": 3,
        "base_delay_seconds": 2,
        "max_delay_seconds": 60,
        "reconcile_before_retry": True,
    }
)

EXECUTOR_BINDING: Mapping[str, str] = MappingProxyType(
    {
        "executor_id": EXECUTOR_ID,
        "executor_binding_version": EXECUTOR_BINDING_VERSION,
    }
)

# Exact, code-owned parameter bounds for the three capacity dimensions. The
# autonomous envelope is a single-instance fleet: floor 0, ceiling 1, step 1.
CAPACITY_BOUNDS: Mapping[str, int] = MappingProxyType(
    {
        "floor": 0,
        "ceiling": 1,
        "max_step": 1,
    }
)

# The exact server-owned preconditions that must hold before the autonomous
# playbook is handed forward. Sorted, deduplicated, and frozen. Note the absence
# of APPROVAL_GRANTED (there is no human in the loop) and the presence of the
# deterministic guardrail preconditions.
PRECONDITIONS: tuple[str, ...] = (
    "ANTI_OSCILLATION_CLEAR",
    "BUDGET_AVAILABLE",
    "CONCURRENCY_AVAILABLE",
    "COOLDOWN_ELAPSED",
    "DECISION_UNEXPIRED",
    "FREQUENCY_AVAILABLE",
    "OBSERVATION_FRESH",
    "RISK_WITHIN_CEILING",
    "TARGET_ENROLLED",
    "WITHIN_CAPACITY_ENVELOPE",
)


def _plain(value: Any) -> Any:
    """Recursively convert MappingProxy/tuples into plain JSON-canonical types."""
    if isinstance(value, Mapping):
        return {key: _plain(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _schema_bindings() -> list[dict[str, str]]:
    """Return sorted hashes for every schema interpreted by this playbook."""
    schemas = [load_schema("common"), load_observation_schema()]
    schemas.extend(load_autonomy_schema(name) for name in sorted(AUTONOMY_SCHEMA_NAMES))
    return sorted(
        (
            {
                "schema_id": schema["$id"],
                "schema_hash": canonical_sha256(schema),
            }
            for schema in schemas
        ),
        key=lambda binding: binding["schema_id"],
    )


def _build_definition() -> dict[str, Any]:
    return {
        "playbook_id": PLAYBOOK_ID,
        "playbook_version": PLAYBOOK_VERSION,
        "profile": PROFILE,
        "action": ACTION,
        "capability": {
            "capability_id": CAPABILITY_ID,
            "capability_version": CAPABILITY_VERSION,
        },
        "schemas": _schema_bindings(),
        "retry_policy": _plain(RETRY_POLICY),
        "capacity_bounds": _plain(CAPACITY_BOUNDS),
        "preconditions": list(PRECONDITIONS),
        "executor_binding": _plain(EXECUTOR_BINDING),
        # The immutable execution authority a future E5 autonomous executor must
        # independently re-verify (mode/policy >= operate) before the single
        # write. Frozen into the playbook so any drift changes the playbook hash.
        "required_execution_authority": REQUIRED_EXECUTION_AUTHORITY,
    }


# The complete, immutable definition the playbook hash is computed over.
AUTONOMY_PLAYBOOK_DEFINITION: dict[str, Any] = _build_definition()


def autonomy_playbook_definition() -> dict[str, Any]:
    """Return a fresh, mutable copy of the immutable autonomy playbook definition."""
    return _build_definition()


def autonomy_playbook_hash() -> str:
    """Return the RFC 8785 / SHA-256 digest of the complete autonomy playbook."""
    return canonical_sha256(AUTONOMY_PLAYBOOK_DEFINITION)
