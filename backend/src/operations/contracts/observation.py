"""Additive read-only observation contract (issue #413, E1 Agent A).

This module validates the ``gamelift-observation`` contract. It is **additive**:
it reuses the immutable ``common`` ``$defs`` but never touches the published
write-contract schema set (``SCHEMA_NAMES``), its validation entry points, or the
source-control playbook binding. Adding an observation schema to ``SCHEMA_NAMES``
would change the playbook hash and break a published v1 write contract, so the
observation schema is loaded and validated through this self-contained path
instead.

The observation is a bounded, read-only GameLift result: fleet utilization,
capacity per location, and scaling policies. It records the verifier-derived
trusted identity, the six ADR 0001 authority inputs, and the deterministic
``effective_authority`` (their minimum), which for the observe phase is capped at
``observe``.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from typing import Any

# Third-party packages
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

# Local modules
from operations.contracts.canonical import CanonicalizationError, canonicalize, load_json

OBSERVATION_SCHEMA_NAME = "gamelift-observation"
COMMON_SCHEMA_NAME = "common"

# The observe phase never grants more than observe authority. The effective
# authority is the deterministic minimum of the six ADR 0001 inputs, but it is
# additionally capped here so an observation can never carry a higher ceiling
# than the phase itself allows.
_OBSERVE_AUTHORITY = "observe"

# ADR 0001 authority lattice ordering; lower is more restrictive.
_AUTHORITY_ORDER = {
    "disabled": 0,
    "observe": 1,
    "advise": 2,
    "remediate": 3,
    "operate": 4,
}

_AUTHORITY_INPUT_FIELDS = (
    "deployment_mode",
    "tenant_policy",
    "workspace_policy",
    "principal_authority",
    "capability_maximum",
    "risk_policy",
)


class ObservationContractError(ValueError):
    """An observation document failed its schema or semantic contract."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"{OBSERVATION_SCHEMA_NAME} contract validation failed: {'; '.join(errors)}")


def _schema_directory():
    return files("operations.contracts").joinpath("schemas", "v1")


@lru_cache(maxsize=None)
def _load_schema_cached(schema_name: str) -> dict[str, Any]:
    schema_path = _schema_directory().joinpath(f"{schema_name}.schema.json")
    document = load_json(schema_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"observation schema must be a JSON object: {schema_name}")
    return document


def load_observation_schema() -> dict[str, Any]:
    """Load a defensive copy of the immutable observation schema."""
    return deepcopy(_load_schema_cached(OBSERVATION_SCHEMA_NAME))


@lru_cache(maxsize=1)
def _observation_registry() -> Registry:
    resources = []
    for schema_name in (OBSERVATION_SCHEMA_NAME, COMMON_SCHEMA_NAME):
        schema = _load_schema_cached(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


def _authority_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    authority_inputs = document["authority_inputs"]
    effective = document["effective_authority"]

    expected = min(
        (authority_inputs[field] for field in _AUTHORITY_INPUT_FIELDS),
        key=_AUTHORITY_ORDER.__getitem__,
    )
    if effective != expected:
        errors.append("effective_authority is not the lowest authority input")

    # The observe phase can never carry more than observe authority.
    if _AUTHORITY_ORDER[effective] > _AUTHORITY_ORDER[_OBSERVE_AUTHORITY]:
        errors.append("effective_authority exceeds the observe phase ceiling")

    # A disabled deployment can never produce an observation result.
    if authority_inputs["deployment_mode"] == "disabled":
        errors.append("disabled deployment mode cannot produce an observation")

    return errors


def _capacity_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen_locations: set[str] = set()
    for index, capacity in enumerate(document["results"]["capacity"]):
        location = capacity["location"]
        if location in seen_locations:
            errors.append(f"results.capacity[{index}] duplicates location {location}")
        seen_locations.add(location)
        if capacity["minimum"] > capacity["maximum"]:
            errors.append(f"results.capacity[{index}] minimum exceeds maximum")
        if not (capacity["minimum"] <= capacity["desired"] <= capacity["maximum"]):
            errors.append(f"results.capacity[{index}] desired is outside the minimum/maximum range")
    return errors


def _scaling_policy_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen_names: set[str] = set()
    for index, policy in enumerate(document["results"]["scaling_policies"]):
        name = policy["name"]
        if name in seen_names:
            errors.append(f"results.scaling_policies[{index}] duplicates name {name}")
        seen_names.add(name)
    return errors


def _identity_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    utilization = document["results"]["utilization"]
    if utilization["current_player_sessions"] > utilization["maximum_player_sessions"]:
        errors.append("results.utilization current_player_sessions exceeds maximum_player_sessions")
    return errors


def _semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(_authority_errors(document))
    errors.extend(_capacity_errors(document))
    errors.extend(_scaling_policy_errors(document))
    errors.extend(_identity_errors(document))
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the observation provider")
    if document["expires_at"] <= document["observed_at"]:
        errors.append("expires_at must be strictly after observed_at")
    return errors


def validate_observation(document: object) -> None:
    """Validate an observation against its schema and semantic invariants."""
    schema = load_observation_schema()
    try:
        canonicalize(document)
    except CanonicalizationError as exc:
        raise ObservationContractError([f"document is outside the canonical I-JSON domain: {exc}"]) from exc

    validator = Draft202012Validator(
        schema,
        registry=_observation_registry(),
        format_checker=FormatChecker(),
    )
    schema_errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    errors = [_format_path(error) for error in schema_errors]

    if not errors and isinstance(document, dict):
        errors.extend(_semantic_errors(document))

    if errors:
        raise ObservationContractError(errors)
