"""Additive E5 bounded-autonomy contract layer (issue #438, E5 operate).

This module validates and binds the three additive **v2** contracts of the
``gamelift.capacity-adjustment/2.0`` capability. They are the bounded-autonomy
(no-human-in-the-loop) counterpart of the E2 prepare/approval contracts in
:mod:`operations.contracts.capacity`:

* ``gamelift-capacity-autonomy-policy`` — the immutable, hash-bound
  bounded-autonomy **policy** envelope. It is entirely server-owned and never
  client, model, or request-body input: it carries the trusted automation
  principal, tenant/workspace/enrollment, the exact ``0/1/1`` capacity envelope,
  the risk ceiling, observation freshness, an integer micro-USD action/window
  budget, cooldown, frequency, ``max_in_flight`` concurrency, anti-oscillation,
  decision expiry, and the exact playbook/executor identities/versions/hashes.
  Its ``policy_hash`` binds every field except itself.
* ``gamelift-capacity-autonomous-decision`` — the deterministic autonomous
  authorization. A non-denied decision is ``authorized`` at ``operate`` authority
  with the single reason code ``APPROVED_AUTONOMOUS``; any guardrail breach
  denies with the specific closed reason code(s). There is no human-approval
  field: the decision itself is the time-boxed grant.
* ``gamelift-capacity-autonomous-operation`` — the immutable, idempotent
  prepared **autonomous** operation. Its ``authority.decision`` enum is the
  ``["authorized","denied"]`` mirror of the v1 ``["approval_required","denied"]``
  prepared operation, and its ``prepared_hash`` binds every other field.

This layer is **v2 and additive**. Every document carries a ``*_contract_version``
of ``"2.0"`` and a distinct ``urn:...:v2:...`` ``$id``; every new ``$def`` is
defined *inline* in the v2 schema files, and only the frozen v1 ``common``
``$defs`` are *referenced* unchanged. The v2 schema names never join the
published write-contract ``SCHEMA_NAMES`` set or the E2/E3 capacity/execution
sets. Adding this layer therefore leaves every published v1 schema, vector, and
meaning byte-for-byte unchanged.

The evaluator here is pure and deterministic. :func:`evaluate_autonomy_policy`
is a total function of trusted inputs only (a resolved policy, a trusted
observation, a proposed capacity triple, the six ADR 0001 authority inputs, and
the durable rolling-window state). It performs no AWS, runtime, or infrastructure
work, reads no environment, holds no credential, and never trusts model or
request-body input for identity, policy, limits, authorization, playbook,
executor, or credentials.
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
from operations.contracts.capacity import calculate_capacity_risk, capacity_change

AUTONOMY_CONTRACT_VERSION = "2.0"

CAPABILITY_ID = "gamelift.capacity-adjustment"
CAPABILITY_VERSION = "2.0"
PROFILE = "gamelift.capacity-adjustment/2.0"
ACTION = "gamelift.adjust-fleet-capacity"
PHASE = "operate"
PROVIDER = "gamelift"

COMMON_SCHEMA_NAME = "common"
AUTONOMY_POLICY_SCHEMA_NAME = "gamelift-capacity-autonomy-policy"
AUTONOMOUS_DECISION_SCHEMA_NAME = "gamelift-capacity-autonomous-decision"
AUTONOMOUS_OPERATION_SCHEMA_NAME = "gamelift-capacity-autonomous-operation"

AUTONOMY_SCHEMA_NAMES = frozenset(
    {
        AUTONOMY_POLICY_SCHEMA_NAME,
        AUTONOMOUS_DECISION_SCHEMA_NAME,
        AUTONOMOUS_OPERATION_SCHEMA_NAME,
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

_RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "critical": 3}

# The minimum effective authority a non-denied autonomous decision requires.
# Unlike the advise-authority E2 phase, bounded autonomy authorizes and executes
# a single provider write with no human in the loop, so it requires the top
# ``operate`` rung of the lattice.
AUTONOMY_MINIMUM_AUTHORITY = "operate"

# The immutable execution authority a future E5 autonomous executor MUST
# independently re-verify (mode/policy >= operate) before the single provider
# write. It is carried, hash-bound, on the policy, the decision, and every
# prepared autonomous operation. There is no human-approval field anywhere in
# this layer: the deterministic decision itself is the time-boxed grant.
REQUIRED_EXECUTION_AUTHORITY = "operate"

# The single reason code carried by a non-denied autonomous decision.
_AUTHORIZED_REASON = "APPROVED_AUTONOMOUS"

# The closed reason-code set the v2 decision/operation enums publish. It is a
# superset of the v1 closed set (the v1 enum is untouched) plus the
# autonomy-specific guardrail codes.
AUTONOMY_REASON_CODES = frozenset(
    {
        "APPROVED_AUTONOMOUS",
        "DEPLOYMENT_DISABLED",
        "INSUFFICIENT_AUTHORITY",
        "POLICY_DENIED",
        "TARGET_NOT_ENROLLED",
        "RISK_LIMIT_EXCEEDED",
        "WORKSPACE_MISMATCH",
        "BOUNDS_EXCEEDED",
        "OBSERVATION_STALE",
        "BUDGET_EXCEEDED",
        "COOLDOWN_ACTIVE",
        "FREQUENCY_EXCEEDED",
        "CONCURRENCY_LIMIT",
        "OSCILLATION_BLOCKED",
        "DECISION_EXPIRED",
        "AUTOMATION_PRINCIPAL_INVALID",
    }
)


class AutonomyContractError(ValueError):
    """An autonomy document failed its schema or semantic contract."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        self.schema_name = schema_name
        self.errors = tuple(errors)
        super().__init__(f"{schema_name} contract validation failed: {'; '.join(errors)}")


def is_supported_autonomy_version(version: str) -> bool:
    """Return whether this package gives the supplied autonomy version meaning.

    The v1 :func:`operations.contracts.versions.is_supported_contract_version`
    check stays ``== "1.0"``; this parallel check is exact at ``"2.0"`` and never
    loosens the v1 allowlist.
    """
    return version == AUTONOMY_CONTRACT_VERSION


def _schema_directory():
    return files("operations.contracts").joinpath("schemas", "v1")


@lru_cache(maxsize=None)
def _load_schema_cached(schema_name: str) -> dict[str, Any]:
    schema_path = _schema_directory().joinpath(f"{schema_name}.schema.json")
    document = load_json(schema_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"autonomy schema must be a JSON object: {schema_name}")
    return document


def load_autonomy_schema(schema_name: str) -> dict[str, Any]:
    """Load a defensive copy of one immutable additive v2 autonomy schema."""
    if schema_name not in AUTONOMY_SCHEMA_NAMES:
        raise ValueError(f"unknown autonomy contract schema: {schema_name}")
    return deepcopy(_load_schema_cached(schema_name))


@lru_cache(maxsize=1)
def _autonomy_registry() -> Registry:
    resources = []
    for schema_name in (COMMON_SCHEMA_NAME, *sorted(AUTONOMY_SCHEMA_NAMES)):
        schema = _load_schema_cached(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


# -- Canonical hashes --------------------------------------------------------


def autonomy_policy_hash(policy: dict[str, Any]) -> str:
    """Return the canonical hash binding every policy field except ``policy_hash``.

    Follows the ``capacity_prepared_hash`` self-excluding pattern: the digest
    covers the whole document minus its own ``policy_hash`` field, so any drift
    in any bound guardrail changes the hash.
    """
    material = {key: value for key, value in policy.items() if key != "policy_hash"}
    return canonical_sha256(material)


def autonomous_decision_hash(decision: dict[str, Any]) -> str:
    """Return the canonical hash of a complete autonomous decision document.

    The decision has no self-referential hash field, so the digest is over the
    entire document (mirrors ``execution_intent_hash``). The prepared autonomous
    operation binds this value in ``decision.decision_hash``.
    """
    return canonical_sha256(decision)


def autonomous_prepared_hash(operation: dict[str, Any]) -> str:
    """Return the canonical hash binding every field except ``prepared_hash``.

    The hash covers the target, the autonomy policy id/version/hash, the bound
    decision id/hash, the current-state observation id/hash, the exact
    desired/min/max change, the playbook/profile/capability/contract versions,
    the authority inputs/decision, the calculated risk, the automation principal
    scope, ``decision_expires_at``, and ``required_execution_authority`` — the
    entire document minus its own ``prepared_hash`` field.
    """
    material = {key: value for key, value in operation.items() if key != "prepared_hash"}
    return canonical_sha256(material)


# -- Semantic validation -----------------------------------------------------


def _policy_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document["capability"]["capability_id"] != CAPABILITY_ID:
        errors.append("capability_id is not the capacity-adjustment capability")
    if document["capability"]["capability_version"] != CAPABILITY_VERSION:
        errors.append("capability_version is not the autonomy capability version")
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the policy provider")
    if document["required_execution_authority"] != REQUIRED_EXECUTION_AUTHORITY:
        errors.append("required_execution_authority must be operate")
    budget = document["budget"]
    if budget["max_action_micro_usd"] > budget["max_window_micro_usd"]:
        errors.append("budget max_action_micro_usd cannot exceed max_window_micro_usd")
    if document["policy_hash"] != autonomy_policy_hash(document):
        errors.append("policy_hash does not bind the canonical policy")
    return errors


def _decision_reason_errors(decision: str, reasons: set[str]) -> list[str]:
    errors: list[str] = []
    if decision == "authorized":
        if reasons != {_AUTHORIZED_REASON}:
            errors.append("an authorized decision must carry only the APPROVED_AUTONOMOUS reason code")
    else:  # denied
        if _AUTHORIZED_REASON in reasons:
            errors.append("a denied decision cannot carry the APPROVED_AUTONOMOUS reason code")
        if not reasons:
            errors.append("a denied decision requires at least one reason code")
    return errors


def _decision_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    authority_inputs = document["authority_inputs"]
    expected_effective = effective_authority(authority_inputs)
    effective = document["effective_authority"]
    decision = document["decision"]
    reasons = set(document["reason_codes"])

    if effective != expected_effective:
        errors.append("effective_authority is not the lowest authority input")

    errors.extend(_decision_reason_errors(decision, reasons))

    if document["capability"]["capability_id"] != CAPABILITY_ID:
        errors.append("capability_id is not the capacity-adjustment capability")
    if document["capability"]["capability_version"] != CAPABILITY_VERSION:
        errors.append("capability_version is not the autonomy capability version")
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the decision provider")
    if document["required_execution_authority"] != REQUIRED_EXECUTION_AUTHORITY:
        errors.append("required_execution_authority must be operate")

    # A non-denied autonomous decision requires the top operate rung; observe /
    # advise / remediate / disabled all deny with INSUFFICIENT_AUTHORITY (or
    # DEPLOYMENT_DISABLED for a disabled deployment).
    if decision == "authorized" and _AUTHORITY_ORDER[effective] < _AUTHORITY_ORDER[AUTONOMY_MINIMUM_AUTHORITY]:
        errors.append("an authorized autonomous decision requires operate authority")

    if authority_inputs["deployment_mode"] == "disabled":
        if decision != "denied":
            errors.append("disabled deployment mode requires a denied decision")
        if "DEPLOYMENT_DISABLED" not in reasons:
            errors.append("disabled deployment mode requires the DEPLOYMENT_DISABLED reason code")

    if document["decision_expires_at"] <= document["evaluated_at"]:
        errors.append("decision_expires_at must be strictly after evaluated_at")
    return errors


def _operation_semantic_errors(document: dict[str, Any]) -> list[str]:
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
    errors.extend(_decision_reason_errors(authority["decision"], reasons))

    if document["capability"]["capability_id"] != CAPABILITY_ID:
        errors.append("capability_id is not the capacity-adjustment capability")
    if document["capability"]["capability_version"] != CAPABILITY_VERSION:
        errors.append("capability_version is not the autonomy capability version")
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the operation provider")
    if document["decision_expires_at"] <= document["created_at"]:
        errors.append("decision_expires_at must be strictly after created_at")

    if document["required_execution_authority"] != REQUIRED_EXECUTION_AUTHORITY:
        errors.append("required_execution_authority must be operate")

    if document["prepared_hash"] != autonomous_prepared_hash(document):
        errors.append("prepared_hash does not bind the canonical prepared operation")
    return errors


_SEMANTIC_VALIDATORS = {
    AUTONOMY_POLICY_SCHEMA_NAME: _policy_semantic_errors,
    AUTONOMOUS_DECISION_SCHEMA_NAME: _decision_semantic_errors,
    AUTONOMOUS_OPERATION_SCHEMA_NAME: _operation_semantic_errors,
}


def validate_autonomy_contract(schema_name: str, document: object) -> None:
    """Validate an autonomy document against its schema and semantic invariants."""
    schema = load_autonomy_schema(schema_name)
    try:
        canonicalize(document)
    except CanonicalizationError as exc:
        raise AutonomyContractError(schema_name, [f"document is outside the canonical I-JSON domain: {exc}"]) from exc

    validator = Draft202012Validator(
        schema,
        registry=_autonomy_registry(),
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
        raise AutonomyContractError(schema_name, errors)


# -- Effective authority -----------------------------------------------------


def effective_authority(authority_inputs: dict[str, str]) -> str:
    """Return the deterministic minimum of the six ADR 0001 authority inputs."""
    return min(
        (authority_inputs[field] for field in _AUTHORITY_INPUT_FIELDS),
        key=_AUTHORITY_ORDER.__getitem__,
    )


# -- Binding -----------------------------------------------------------------


def validate_autonomous_operation_binding(
    operation: dict[str, Any],
    decision: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    """Verify one prepared autonomous operation binds one exact decision + policy.

    All three documents must be contract-valid, and the operation must bind the
    decision by id and canonical hash, bind the policy by id/version/hash, and
    agree on the automation principal scope, target, authority, risk, decision
    expiry, and execution authority. This is the E5 analogue of
    ``validate_prepared_operation_binding`` for the no-human-approval path.
    """
    validate_autonomy_contract(AUTONOMOUS_OPERATION_SCHEMA_NAME, operation)
    validate_autonomy_contract(AUTONOMOUS_DECISION_SCHEMA_NAME, decision)
    validate_autonomy_contract(AUTONOMY_POLICY_SCHEMA_NAME, policy)

    errors: list[str] = []
    if operation["decision"]["decision_id"] != decision["decision_id"]:
        errors.append("operation decision_id does not match the autonomous decision")
    if operation["decision"]["decision_hash"] != autonomous_decision_hash(decision):
        errors.append("operation decision_hash does not match the canonical decision")
    if operation["autonomy_policy"]["policy_id"] != policy["policy_id"]:
        errors.append("operation policy_id does not match the autonomy policy")
    if operation["autonomy_policy"]["policy_version"] != policy["policy_version"]:
        errors.append("operation policy_version does not match the autonomy policy")
    if operation["autonomy_policy"]["policy_hash"] != policy["policy_hash"]:
        errors.append("operation policy_hash does not match the autonomy policy")
    if decision["policy"]["policy_hash"] != policy["policy_hash"]:
        errors.append("decision policy_hash does not match the autonomy policy")

    op_principal = operation["automation_principal"]
    if (op_principal["subject_id"], op_principal["client_id"]) != (
        decision["automation_principal"]["subject_id"],
        decision["automation_principal"]["client_id"],
    ):
        errors.append("operation automation_principal does not match the decision")
    if (op_principal["tenant_id"], op_principal["workspace_id"]) != (policy["tenant_id"], policy["workspace_id"]):
        errors.append("operation automation_principal scope does not match the policy")

    if operation["target"] != decision["target"]:
        errors.append("operation target does not match the decision target")
    if operation["authority"]["decision"] != decision["decision"]:
        errors.append("operation authority decision does not match the decision")
    if operation["authority"]["effective_authority"] != decision["effective_authority"]:
        errors.append("operation effective_authority does not match the decision")
    if operation["authority"]["authority_inputs"] != decision["authority_inputs"]:
        errors.append("operation authority_inputs do not match the decision")
    if operation["decision_expires_at"] != decision["decision_expires_at"]:
        errors.append("operation decision_expires_at does not match the decision")
    if operation["required_execution_authority"] != decision["required_execution_authority"]:
        errors.append("operation required_execution_authority does not match the decision")
    if errors:
        raise AutonomyContractError("gamelift-capacity-autonomous-operation-binding", errors)


# -- Pure deterministic policy evaluator -------------------------------------


def _bounds_violations(*, requested: dict[str, int], change: dict[str, int], bounds: dict[str, int]) -> bool:
    """Return whether the request violates the exact 0/1/1 capacity envelope."""
    if requested["minimum"] > requested["maximum"]:
        return True
    if not (requested["minimum"] <= requested["desired"] <= requested["maximum"]):
        return True
    if requested["desired"] < bounds["floor"] or requested["desired"] > bounds["ceiling"]:
        return True
    if requested["minimum"] < bounds["floor"] or requested["maximum"] > bounds["ceiling"]:
        return True
    if abs(change["desired"]) > bounds["max_step"]:
        return True
    return False


def evaluate_autonomy_policy(
    *,
    policy: dict[str, Any],
    authority_inputs: dict[str, str],
    automation_principal: dict[str, str],
    observation: dict[str, Any],
    requested: dict[str, int],
    window_state: dict[str, Any],
    now_epoch_seconds: int,
) -> dict[str, Any]:
    """Deterministically decide whether one autonomous capacity write is allowed.

    Pure total function of trusted inputs only. It performs no AWS, runtime, or
    infrastructure work, reads no environment, holds no credential, and never
    consults model or request-body input for identity, policy, limits,
    authorization, playbook, executor, or credentials. Identical inputs always
    yield an identical ``(decision, reason_codes, effective_authority,
    calculated_risk)`` reading, and the caller binds that reading into the
    hash-bound decision document.

    ``window_state`` is the durable, server-owned rolling-window reading the
    caller supplies (``last_write_epoch_seconds`` or ``None``,
    ``writes_in_window``, ``window_micro_usd``, ``in_flight``,
    ``seconds_since_opposite_change`` or ``None``, ``direction_flips_in_window``).
    All comparisons use integers; there is no floating-point money.
    """
    current = observation["capacity"]
    change = capacity_change(current, requested)
    within_bounds = not _bounds_violations(requested=requested, change=change, bounds=policy["capacity_bounds"])
    calculated_risk = calculate_capacity_risk(current=current, requested=requested, within_bounds=within_bounds)

    effective = effective_authority(authority_inputs)
    reasons: list[str] = []

    # Deployment/authority gates first; a disabled deployment denies unconditionally.
    if authority_inputs["deployment_mode"] == "disabled":
        reasons.append("DEPLOYMENT_DISABLED")
    if _AUTHORITY_ORDER[effective] < _AUTHORITY_ORDER[AUTONOMY_MINIMUM_AUTHORITY]:
        reasons.append("INSUFFICIENT_AUTHORITY")

    # Trusted automation principal must match the policy exactly. Model/untrusted
    # input can never supply this: it comes from the verified principal.
    expected_principal = policy["automation_principal"]
    if (
        automation_principal.get("source_type") != "automation"
        or automation_principal.get("subject_id") != expected_principal["subject_id"]
        or automation_principal.get("client_id") != expected_principal["client_id"]
    ):
        reasons.append("AUTOMATION_PRINCIPAL_INVALID")

    # Workspace scope (tenant/workspace are policy-owned).
    if (observation.get("tenant_id"), observation.get("workspace_id")) != (
        policy["tenant_id"],
        policy["workspace_id"],
    ):
        reasons.append("WORKSPACE_MISMATCH")

    # Enrollment / target.
    if not observation.get("target_enrolled", False):
        reasons.append("TARGET_NOT_ENROLLED")

    if not within_bounds:
        reasons.append("BOUNDS_EXCEEDED")

    # Risk ceiling.
    risk_limit = policy["risk"]
    if (
        _RISK_ORDER[calculated_risk["level"]] > _RISK_ORDER[risk_limit["max_level"]]
        or calculated_risk["score"] > risk_limit["max_score"]
    ):
        reasons.append("RISK_LIMIT_EXCEEDED")

    # Observation freshness / skew.
    freshness = policy["observation_freshness"]
    age = now_epoch_seconds - int(observation["observed_at_epoch_seconds"])
    if age > freshness["max_age_seconds"] or age < -freshness["max_skew_seconds"]:
        reasons.append("OBSERVATION_STALE")

    # Budget (integer micro-USD, per-action and per-window).
    budget = policy["budget"]
    action_cost = int(window_state.get("action_micro_usd", 0))
    if action_cost > budget["max_action_micro_usd"]:
        reasons.append("BUDGET_EXCEEDED")
    elif int(window_state.get("window_micro_usd", 0)) + action_cost > budget["max_window_micro_usd"]:
        reasons.append("BUDGET_EXCEEDED")

    # Cooldown since the last successful autonomous write.
    last_write = window_state.get("last_write_epoch_seconds")
    if (
        last_write is not None
        and (now_epoch_seconds - int(last_write)) < policy["cooldown"]["min_seconds_between_writes"]
    ):
        reasons.append("COOLDOWN_ACTIVE")

    # Frequency cap (writes per window).
    if int(window_state.get("writes_in_window", 0)) >= policy["frequency"]["max_writes_per_window"]:
        reasons.append("FREQUENCY_EXCEEDED")

    # Concurrency (max_in_flight == 1).
    if int(window_state.get("in_flight", 0)) >= policy["concurrency"]["max_in_flight"]:
        reasons.append("CONCURRENCY_LIMIT")

    # Anti-oscillation: block a change that reverses a recent opposite change,
    # or exceeds the allowed direction flips in the window.
    anti = policy["anti_oscillation"]
    since_opposite = window_state.get("seconds_since_opposite_change")
    if since_opposite is not None and int(since_opposite) < anti["min_seconds_since_opposite_change"]:
        reasons.append("OSCILLATION_BLOCKED")
    elif int(window_state.get("direction_flips_in_window", 0)) > anti["max_direction_flips_per_window"]:
        reasons.append("OSCILLATION_BLOCKED")

    unique_reasons = sorted(set(reasons))
    if unique_reasons:
        decision = "denied"
        reason_codes = unique_reasons
    else:
        decision = "authorized"
        reason_codes = [_AUTHORIZED_REASON]

    return {
        "decision": decision,
        "reason_codes": reason_codes,
        "effective_authority": effective,
        "calculated_risk": calculated_risk,
        "change": change,
        "within_bounds": within_bounds,
    }
