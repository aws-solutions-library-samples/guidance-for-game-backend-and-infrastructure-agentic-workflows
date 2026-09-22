"""Code-owned, immutable GameLift capacity playbook definition (issue #414).

The E2 prepared operation binds a ``playbook_hash`` — a stable, deterministic
digest of the *complete* trusted playbook the deployment will hand forward to a
future executor. Before live deployment this hash must be a real RFC 8785 /
SHA-256 digest of the entire immutable definition (never an all-zero
placeholder), so that any drift in the playbook — its identity, version,
profile, capability, retry policy, parameter/precondition bounds, or executor
binding — changes the hash and is caught by tests.

The definition here is the single source of truth. ``CapacityPlaybook`` in
``operations.prepare`` is constructed from these values, and
``capacity_playbook_hash()`` computes the digest the bootstrap embeds. Nothing
in this module reads the environment, holds a credential, or performs I/O.
"""

from __future__ import annotations

# Standard library
from types import MappingProxyType
from typing import Any, Mapping

# Local modules
from operations.contracts.canonical import canonical_sha256
from operations.contracts.capacity import ACTION, CAPABILITY_ID, CAPABILITY_VERSION, PROFILE

PLAYBOOK_ID = "playbook.gamelift-capacity"
PLAYBOOK_VERSION = "1.0.0"

EXECUTOR_ID = "executor.gamelift-capacity"
EXECUTOR_BINDING_VERSION = "1.0"

# The retry policy the future executor honors. Frozen so a change is a
# deliberate, hash-visible playbook revision.
RETRY_POLICY: Mapping[str, Any] = MappingProxyType(
    {
        "max_attempts": 3,
        "base_delay_seconds": 2,
        "max_delay_seconds": 60,
        "reconcile_before_retry": True,
    }
)

FUTURE_EXECUTOR_BINDING: Mapping[str, str] = MappingProxyType(
    {
        "executor_id": EXECUTOR_ID,
        "executor_binding_version": EXECUTOR_BINDING_VERSION,
    }
)

# Exact, code-owned parameter bounds for the three capacity dimensions. These
# are the structural envelope the playbook accepts; the *deployment*-configured
# fail-closed limits (floor/ceiling/max_step) are resolved separately at
# bootstrap and are always at or inside this envelope.
PARAMETER_BOUNDS: Mapping[str, Mapping[str, int]] = MappingProxyType(
    {
        "desired": MappingProxyType({"minimum": 0, "maximum": 1_000_000}),
        "minimum": MappingProxyType({"minimum": 0, "maximum": 1_000_000}),
        "maximum": MappingProxyType({"minimum": 0, "maximum": 1_000_000}),
    }
)

# The exact server-owned preconditions that must hold before the playbook is
# handed forward. Sorted, deduplicated, and frozen.
PRECONDITIONS: tuple[str, ...] = (
    "APPROVAL_GRANTED",
    "OBSERVATION_FRESH",
    "TARGET_ENROLLED",
    "WITHIN_SERVER_BOUNDS",
)


def _plain(value: Any) -> Any:
    """Recursively convert MappingProxy/tuples into plain JSON-canonical types."""
    if isinstance(value, Mapping):
        return {key: _plain(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


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
        "retry_policy": _plain(RETRY_POLICY),
        "parameter_bounds": _plain(PARAMETER_BOUNDS),
        "preconditions": list(PRECONDITIONS),
        "future_executor_binding": _plain(FUTURE_EXECUTOR_BINDING),
    }


# The complete, immutable definition the playbook hash is computed over.
CAPACITY_PLAYBOOK_DEFINITION: dict[str, Any] = _build_definition()


def capacity_playbook_definition() -> dict[str, Any]:
    """Return a fresh, mutable copy of the immutable playbook definition."""
    return _build_definition()


def capacity_playbook_hash() -> str:
    """Return the RFC 8785 / SHA-256 digest of the complete playbook definition."""
    return canonical_sha256(CAPACITY_PLAYBOOK_DEFINITION)
