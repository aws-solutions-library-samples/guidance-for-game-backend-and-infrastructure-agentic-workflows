"""Additive E2 capacity-adjustment contract layer (issue #414, E2 prepare).

This module validates and binds the four additive contracts of the
``gamelift.capacity-adjustment/1.0`` capability:

* ``gamelift-capacity-proposal-request`` — untrusted input. It carries only
  bounded target/requested capacity intent and an idempotency token. It has no
  identity, correlation, policy, enrollment, risk, executor, credential,
  approval, deployment-mode, playbook, or trusted current-state field, and
  ``additionalProperties`` is false everywhere, so injecting any such field
  fails validation.
* ``gamelift-capacity-advice`` — deterministic advice. It is a pure, server
  computed reading of one proposal against one trusted E1 observation: the exact
  desired/min/max change, the server-owned bounds outcome, and deterministic
  risk. No model free-text ever enters it.
* ``gamelift-capacity-authorization`` — the trusted authority decision. A
  non-denied E2 GameLift capacity decision is always ``approval_required``: a
  direct human approval can never be bypassed.
* ``gamelift-capacity-prepared-operation`` — the immutable, idempotent prepared
  operation. Its ``prepared_hash`` binds every other field (and excludes only
  itself), including the target, the current-state observation id/hash, the
  exact desired/min/max change, the playbook/profile/capability/contract
  versions, the authority inputs/decision, the calculated risk, the requester
  scope, the expiry, and the future executor binding identifier.

Like the E1 observation contract, this layer is **additive**: it reuses the
immutable ``common`` ``$defs`` but never joins the published write-contract
``SCHEMA_NAMES`` set, its validators, or the source-control playbook binding, so
it cannot change a published v1 write contract or its playbook hash. Structural
absence is enforced by schema: there is no provider-write parameter, and the
future executor binding is an identifier only — never a credential.
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
from operations.contracts.canonical import CanonicalizationError, canonical_sha256, canonicalize, load_json

CAPABILITY_ID = "gamelift.capacity-adjustment"
CAPABILITY_VERSION = "1.0"
PROFILE = "gamelift.capacity-adjustment/1.0"
ACTION = "gamelift.adjust-fleet-capacity"
PHASE = "advise"
PROVIDER = "gamelift"

COMMON_SCHEMA_NAME = "common"
PROPOSAL_REQUEST_SCHEMA_NAME = "gamelift-capacity-proposal-request"
ADVICE_SCHEMA_NAME = "gamelift-capacity-advice"
AUTHORIZATION_SCHEMA_NAME = "gamelift-capacity-authorization"
PREPARED_OPERATION_SCHEMA_NAME = "gamelift-capacity-prepared-operation"

CAPACITY_SCHEMA_NAMES = frozenset(
    {
        PROPOSAL_REQUEST_SCHEMA_NAME,
        ADVICE_SCHEMA_NAME,
        AUTHORIZATION_SCHEMA_NAME,
        PREPARED_OPERATION_SCHEMA_NAME,
    }
)

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
    "operation_risk_policy",
)

# A GameLift capacity change is a remediate-class action; it can never be
# authorized outright, only prepared for a direct human approval.
_APPROVAL_ONLY_DECISION = "approval_required"


class CapacityContractError(ValueError):
    """A capacity document failed its schema or semantic contract."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        self.schema_name = schema_name
        self.errors = tuple(errors)
        super().__init__(f"{schema_name} contract validation failed: {'; '.join(errors)}")


def _schema_directory():
    return files("operations.contracts").joinpath("schemas", "v1")


@lru_cache(maxsize=None)
def _load_schema_cached(schema_name: str) -> dict[str, Any]:
    schema_path = _schema_directory().joinpath(f"{schema_name}.schema.json")
    document = load_json(schema_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"capacity schema must be a JSON object: {schema_name}")
    return document


def load_capacity_schema(schema_name: str) -> dict[str, Any]:
    """Load a defensive copy of one immutable additive capacity schema."""
    if schema_name not in CAPACITY_SCHEMA_NAMES:
        raise ValueError(f"unknown capacity contract schema: {schema_name}")
    return deepcopy(_load_schema_cached(schema_name))


@lru_cache(maxsize=1)
def _capacity_registry() -> Registry:
    resources = []
    for schema_name in (COMMON_SCHEMA_NAME, *sorted(CAPACITY_SCHEMA_NAMES)):
        schema = _load_schema_cached(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


# -- Deterministic change / risk -------------------------------------------


def capacity_change(current: dict[str, int], requested: dict[str, int]) -> dict[str, int]:
    """Return the exact desired/min/max delta between current and requested."""
    return {
        "desired": requested["desired"] - current["desired"],
        "minimum": requested["minimum"] - current["minimum"],
        "maximum": requested["maximum"] - current["maximum"],
    }


def _risk_level_for_score(score: int) -> str:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "moderate"
    return "low"


def calculate_capacity_risk(
    *,
    current: dict[str, int],
    requested: dict[str, int],
    within_bounds: bool,
) -> dict[str, Any]:
    """Deterministically score the risk of one capacity change.

    The score is a pure function of the numeric change and the server-owned
    bounds outcome. It never depends on model text, wall-clock time, or any
    identity, so an identical proposal against identical current state always
    yields an identical risk.
    """
    change = capacity_change(current, requested)
    factors: list[str] = []
    score = 0

    desired_delta = abs(change["desired"])
    baseline = max(current["desired"], 1)
    # Relative magnitude of the desired change, capped so a single factor can
    # never dominate the bounded [0, 100] score.
    magnitude = min((desired_delta * 50) // baseline, 50)
    if magnitude:
        score += magnitude
        factors.append("desired-magnitude")

    if change["desired"] > 0 or change["maximum"] > 0:
        score += 10
        factors.append("scale-up")
    if change["desired"] < 0 or change["minimum"] < 0:
        score += 15
        factors.append("scale-down")
    if not within_bounds:
        score += 40
        factors.append("bounds-violation")

    score = max(0, min(score, 100))
    return {
        "level": _risk_level_for_score(score),
        "score": score,
        "factors": sorted(set(factors)),
    }


# -- Bounds -----------------------------------------------------------------


def capacity_bounds_violations(
    *,
    requested: dict[str, int],
    change: dict[str, int],
    limits: dict[str, int],
    target_enrolled: bool,
) -> list[str]:
    """Return the sorted, deduplicated server-owned bounds violations.

    ``limits`` are server-owned enrollment/policy bounds — never client input.
    """
    violations: set[str] = set()

    if not target_enrolled:
        violations.add("TARGET_NOT_ENROLLED")
    if requested["minimum"] > requested["maximum"]:
        violations.add("MINIMUM_EXCEEDS_MAXIMUM")
    if not (requested["minimum"] <= requested["desired"] <= requested["maximum"]):
        violations.add("DESIRED_OUTSIDE_RANGE")
    if requested["desired"] < limits["floor"]:
        violations.add("DESIRED_BELOW_FLOOR")
    if requested["desired"] > limits["ceiling"]:
        violations.add("DESIRED_ABOVE_CEILING")
    if requested["minimum"] < limits["floor"]:
        violations.add("MINIMUM_BELOW_FLOOR")
    if requested["maximum"] > limits["ceiling"]:
        violations.add("MAXIMUM_ABOVE_CEILING")
    if abs(change["desired"]) > limits["max_step"]:
        violations.add("DELTA_EXCEEDS_MAX_STEP")

    return sorted(violations)


# -- Effective authority / decision ----------------------------------------


def effective_authority(authority_inputs: dict[str, str]) -> str:
    """Return the deterministic minimum of the six ADR 0001 authority inputs."""
    return min(
        (authority_inputs[field] for field in _AUTHORITY_INPUT_FIELDS),
        key=_AUTHORITY_ORDER.__getitem__,
    )


# -- Prepared hash ----------------------------------------------------------


def capacity_prepared_hash(prepared_operation: dict[str, Any]) -> str:
    """Return the canonical hash that binds every field except ``prepared_hash``.

    The hash covers the target, the current-state observation revision/hash, the
    exact desired/min/max change, the playbook/profile/capability/contract
    versions, the authority inputs/decision, the calculated risk, the requester
    scope, the expiry, and the future executor binding identifier — the entire
    document minus its own ``prepared_hash`` field.
    """
    material = {key: value for key, value in prepared_operation.items() if key != "prepared_hash"}
    return canonical_sha256(material)


# -- Semantic validation ----------------------------------------------------


def _advice_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    current = document["current_state"]["capacity"]
    requested = document["requested"]
    expected_change = capacity_change(current, requested)
    if document["change"] != expected_change:
        errors.append("change is not the exact requested-minus-current delta")

    bounds = document["bounds"]
    if bounds["within_bounds"] != (not bounds["violations"]):
        errors.append("bounds.within_bounds does not match the presence of violations")
    if document["capability"]["capability_id"] != CAPABILITY_ID:
        errors.append("capability_id is not the capacity-adjustment capability")
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the advice provider")
    return errors


def _authorization_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    authority_inputs = document["authority_inputs"]
    expected = effective_authority(authority_inputs)
    effective = document["effective_authority"]
    decision = document["decision"]
    reasons = set(document["reason_codes"])

    if effective != expected:
        errors.append("effective_authority is not the lowest authority input")

    # A GameLift capacity change is never authorized outright.
    if decision == "authorized":
        errors.append("gamelift capacity decisions cannot be authorized without approval")
    elif decision == "approval_required" and reasons != {"APPROVAL_REQUIRED"}:
        errors.append("approval_required decision must have only the APPROVAL_REQUIRED reason code")
    elif decision == "denied" and "APPROVAL_REQUIRED" in reasons:
        errors.append("denied decision cannot carry the APPROVAL_REQUIRED reason code")

    if decision != "denied" and _AUTHORITY_ORDER[effective] < _AUTHORITY_ORDER["remediate"]:
        errors.append("a non-denied capacity decision requires at least remediate authority")

    if authority_inputs["deployment_mode"] == "disabled":
        if decision != "denied":
            errors.append("disabled deployment mode requires a denied decision")
        if "DEPLOYMENT_DISABLED" not in reasons:
            errors.append("disabled deployment mode requires the DEPLOYMENT_DISABLED reason code")

    if document["expires_at"] <= document["evaluated_at"]:
        errors.append("expires_at must be strictly after evaluated_at")
    return errors


def _prepared_operation_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    current = document["current_state"]["capacity"]
    requested = document["parameters"]["requested"]
    expected_change = capacity_change(current, requested)
    if document["parameters"]["change"] != expected_change:
        errors.append("parameters.change is not the exact requested-minus-current delta")

    authority = document["authority"]
    expected_effective = effective_authority(authority["authority_inputs"])
    if authority["effective_authority"] != expected_effective:
        errors.append("authority.effective_authority is not the lowest authority input")

    reasons = set(authority["reason_codes"])
    if authority["decision"] == "approval_required" and reasons != {"APPROVAL_REQUIRED"}:
        errors.append("approval_required decision must have only the APPROVAL_REQUIRED reason code")
    if authority["decision"] == "denied" and "APPROVAL_REQUIRED" in reasons:
        errors.append("denied decision cannot carry the APPROVAL_REQUIRED reason code")

    if document["capability"]["capability_id"] != CAPABILITY_ID:
        errors.append("capability_id is not the capacity-adjustment capability")
    if document["capability"]["capability_version"] != CAPABILITY_VERSION:
        errors.append("capability_version is not the capacity-adjustment version")
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the operation provider")
    if document["expires_at"] <= document["created_at"]:
        errors.append("expires_at must be strictly after created_at")

    if document["prepared_hash"] != capacity_prepared_hash(document):
        errors.append("prepared_hash does not bind the canonical prepared operation")
    return errors


_SEMANTIC_VALIDATORS = {
    ADVICE_SCHEMA_NAME: _advice_semantic_errors,
    AUTHORIZATION_SCHEMA_NAME: _authorization_semantic_errors,
    PREPARED_OPERATION_SCHEMA_NAME: _prepared_operation_semantic_errors,
}


def validate_capacity_contract(schema_name: str, document: object) -> None:
    """Validate a capacity document against its schema and semantic invariants."""
    schema = load_capacity_schema(schema_name)
    try:
        canonicalize(document)
    except CanonicalizationError as exc:
        raise CapacityContractError(schema_name, [f"document is outside the canonical I-JSON domain: {exc}"]) from exc

    validator = Draft202012Validator(
        schema,
        registry=_capacity_registry(),
        format_checker=FormatChecker(),
    )
    schema_errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    errors = [_format_path(error) for error in schema_errors]

    if not errors and isinstance(document, dict):
        semantic = _SEMANTIC_VALIDATORS.get(schema_name)
        if semantic is not None:
            errors.extend(semantic(document))

    if errors:
        raise CapacityContractError(schema_name, errors)


def validate_prepared_operation_binding(
    prepared_operation: dict[str, Any],
    authorization: dict[str, Any],
) -> None:
    """Verify one authorization decision against one prepared capacity operation."""
    validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, prepared_operation)
    validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authorization)

    errors: list[str] = []
    if authorization["prepared_operation_hash"] != prepared_operation["prepared_hash"]:
        errors.append("authorization hash does not match the prepared operation hash")
    if authorization["principal"] != prepared_operation["requester"]:
        errors.append("authorization principal does not match the prepared operation requester")
    if authorization["policy_version"] != prepared_operation["policy"]["policy_version"]:
        errors.append("authorization policy_version does not match the prepared operation")
    if authorization["correlation"] != prepared_operation["correlation"]:
        errors.append("authorization correlation does not match the prepared operation")
    if authorization["decision"] != prepared_operation["authority"]["decision"]:
        errors.append("authorization decision does not match the prepared operation authority")
    if authorization["effective_authority"] != prepared_operation["authority"]["effective_authority"]:
        errors.append("authorization effective_authority does not match the prepared operation authority")
    if authorization["authority_inputs"] != prepared_operation["authority"]["authority_inputs"]:
        errors.append("authorization authority_inputs do not match the prepared operation authority")
    if errors:
        raise CapacityContractError("gamelift-capacity-authorization-binding", errors)
