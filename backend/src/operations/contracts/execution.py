"""Additive E3 capacity-execution contract layer (issue #415, E3 execute).

This module validates and binds the three additive execution contracts of the
``gamelift.capacity-adjustment/1.0`` capability. They are the E3 execution-side
counterpart of the E2 prepare/approval contracts in
:mod:`operations.contracts.capacity`:

* ``gamelift-capacity-execution-intent`` — the server-owned provider-write
  intent. It is derived *deterministically* from one immutable E2 prepared
  operation and carries only the exact ``UpdateFleetCapacity`` parameters, the
  target, the prepared operation id, the ``prepared_hash`` it binds to, the
  hash-bound expected current capacity, and a stable, attempt-independent
  ``logical_action_id``. It never carries identity, a credential, a provider
  response, an ARN, an account id, or executable content.
* ``gamelift-capacity-execution-verification`` — the bounded post-action
  ``DescribeFleetCapacity`` reading against the hash-bound expected target.
* ``gamelift-capacity-execution-result`` — the normalized, bounded terminal
  outcome of at most one logical write (or a reconciliation with no write).

Like the E2 capacity layer, this layer is **additive**: it reuses the immutable
``common`` ``$defs`` but never joins the published write-contract
``SCHEMA_NAMES`` set, its validators, or the source-control playbook binding, so
it cannot change a published v1 write contract or its playbook hash. There is no
provider-write parameter beyond the bounded capacity values, and no executor
credential anywhere.

Determinism
-----------

``build_execution_intent`` is a pure function of the prepared operation: the
same prepared operation always yields a byte-for-byte identical intent, and the
``logical_action_id`` is ``act_`` + the SHA-256 of the canonical
``(operation_id, prepared_hash)`` pair — independent of any execution attempt.
This guarantees one operation maps to exactly one logical update regardless of
how many times the executor is (re-)invoked.
"""

from __future__ import annotations

# Standard library
import hashlib
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from typing import Any

# Third-party packages
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

# Local modules
from operations.contracts.canonical import CanonicalizationError, canonical_sha256, canonicalize, load_json
from operations.contracts.capacity import CAPABILITY_VERSION, capacity_prepared_hash

CONTRACT_VERSION = "1.0"
PROVIDER = "gamelift"
ACTION = "gamelift.adjust-fleet-capacity"

COMMON_SCHEMA_NAME = "common"
EXECUTION_INTENT_SCHEMA_NAME = "gamelift-capacity-execution-intent"
EXECUTION_VERIFICATION_SCHEMA_NAME = "gamelift-capacity-execution-verification"
EXECUTION_RESULT_SCHEMA_NAME = "gamelift-capacity-execution-result"

EXECUTION_SCHEMA_NAMES = frozenset(
    {
        EXECUTION_INTENT_SCHEMA_NAME,
        EXECUTION_VERIFICATION_SCHEMA_NAME,
        EXECUTION_RESULT_SCHEMA_NAME,
    }
)

# Terminal execution outcomes (mirrors the result schema enum).
OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_RECONCILED = "RECONCILED"
OUTCOME_FAILED = "FAILED"
OUTCOME_HUMAN_RECONCILIATION_REQUIRED = "HUMAN_RECONCILIATION_REQUIRED"


class ExecutionContractError(ValueError):
    """An execution document failed its schema or semantic contract."""

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
        raise ValueError(f"execution schema must be a JSON object: {schema_name}")
    return document


def load_execution_schema(schema_name: str) -> dict[str, Any]:
    """Load a defensive copy of one immutable additive execution schema."""
    if schema_name not in EXECUTION_SCHEMA_NAMES:
        raise ValueError(f"unknown execution contract schema: {schema_name}")
    return deepcopy(_load_schema_cached(schema_name))


@lru_cache(maxsize=1)
def _execution_registry() -> Registry:
    resources = []
    # The result schema $refs the verification schema, so both plus common must
    # be resolvable in the registry.
    for schema_name in (COMMON_SCHEMA_NAME, *sorted(EXECUTION_SCHEMA_NAMES)):
        schema = _load_schema_cached(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


# -- Logical action id / intent hash ----------------------------------------


def logical_action_id(operation_id: str, prepared_hash: str) -> str:
    """Return the stable, attempt-independent logical action id.

    It is ``act_`` + the SHA-256 of the canonical ``(operation_id,
    prepared_hash)`` pair. It never depends on an attempt counter, wall clock,
    or random value, so every (re-)invocation for the same operation computes
    the same id and one operation maps to exactly one logical update.
    """
    material = canonical_sha256({"operation_id": operation_id, "prepared_hash": prepared_hash})
    # ``canonical_sha256`` returns a ``sha256:`` prefixed hex digest; re-hash
    # the digest bytes to a bare 64-hex action token.
    digest = hashlib.sha256(material.encode("ascii")).hexdigest()
    return f"act_{digest}"


def execution_intent_hash(intent: dict[str, Any]) -> str:
    """Return the canonical hash of a complete execution intent document."""
    return canonical_sha256(intent)


# -- Intent construction -----------------------------------------------------


def build_execution_intent(prepared_operation: dict[str, Any]) -> dict[str, Any]:
    """Deterministically derive the server-owned write intent from one prepared op.

    Pure function: the same prepared operation always yields a byte-for-byte
    identical intent. No identity, credential, attempt counter, wall clock, or
    random value enters the result.
    """
    prepared_hash = capacity_prepared_hash(prepared_operation)
    operation_id = prepared_operation["operation_id"]
    return {
        "execution_contract_version": CONTRACT_VERSION,
        "operation_id": operation_id,
        "prepared_hash": prepared_hash,
        "logical_action_id": logical_action_id(operation_id, prepared_hash),
        "provider": PROVIDER,
        "action": ACTION,
        "target": deepcopy(prepared_operation["target"]),
        "parameters": deepcopy(prepared_operation["parameters"]["requested"]),
        "expected_current_capacity": deepcopy(prepared_operation["current_state"]["capacity"]),
    }


# -- Semantic validation -----------------------------------------------------


def _intent_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document["target"]["provider"] != document["provider"]:
        errors.append("target.provider does not match the intent provider")
    expected_action_id = logical_action_id(document["operation_id"], document["prepared_hash"])
    if document["logical_action_id"] != expected_action_id:
        errors.append("logical_action_id is not the deterministic (operation_id, prepared_hash) digest")
    return errors


def _verification_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    matches = document["observed_capacity"] == document["expected_capacity"]
    if document["matches_target"] != matches:
        errors.append("matches_target does not reflect observed vs expected capacity")
    return errors


def _result_semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    outcome = document["outcome"]
    verification = document["verification"]
    if verification["operation_id"] != document["operation_id"]:
        errors.append("verification operation_id does not match the result")
    if verification["logical_action_id"] != document["logical_action_id"]:
        errors.append("verification logical_action_id does not match the result")

    if outcome == OUTCOME_RECONCILED and document["provider_write_issued"]:
        errors.append("a reconciled outcome must not issue a provider write")
    if outcome == OUTCOME_SUCCEEDED:
        if not document["provider_write_issued"]:
            errors.append("a succeeded outcome requires exactly one provider write")
        if not verification["matches_target"]:
            errors.append("a succeeded outcome requires verification to match the target")
    if outcome in (OUTCOME_FAILED, OUTCOME_HUMAN_RECONCILIATION_REQUIRED):
        if "failure_reason_code" not in document:
            errors.append("a failed or reconciliation-required outcome requires a failure_reason_code")
    return errors


_SEMANTIC_VALIDATORS = {
    EXECUTION_INTENT_SCHEMA_NAME: _intent_semantic_errors,
    EXECUTION_VERIFICATION_SCHEMA_NAME: _verification_semantic_errors,
    EXECUTION_RESULT_SCHEMA_NAME: _result_semantic_errors,
}


def validate_execution_contract(schema_name: str, document: object) -> None:
    """Validate an execution document against its schema and semantic invariants."""
    schema = load_execution_schema(schema_name)
    try:
        canonicalize(document)
    except CanonicalizationError as exc:
        raise ExecutionContractError(schema_name, [f"document is outside the canonical I-JSON domain: {exc}"]) from exc

    validator = Draft202012Validator(
        schema,
        registry=_execution_registry(),
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
        raise ExecutionContractError(schema_name, errors)


def validate_execution_intent_binding(intent: dict[str, Any], prepared_operation: dict[str, Any]) -> None:
    """Verify one execution intent binds one exact stored prepared operation.

    The intent's ``prepared_hash`` must equal the canonical prepared hash of the
    supplied operation, its ``operation_id`` and ``target`` must match, and its
    write parameters and expected current capacity must be exactly the prepared
    requested capacity and current state — so no drift or tamper between prepare
    and execute can widen or redirect the write.
    """
    # Local modules — imported lazily to avoid a cycle with the capacity layer.
    # Local modules
    from operations.contracts.capacity import CapacityContractError, validate_capacity_prepared_operation

    validate_execution_contract(EXECUTION_INTENT_SCHEMA_NAME, intent)
    try:
        validate_capacity_prepared_operation(prepared_operation)
    except CapacityContractError as exc:
        # A tampered or malformed prepared operation is itself a binding failure.
        raise ExecutionContractError(
            "gamelift-capacity-execution-intent-binding",
            ["bound prepared operation is not contract-valid: " + "; ".join(exc.errors)],
        ) from exc

    errors: list[str] = []
    prepared_hash = capacity_prepared_hash(prepared_operation)
    if intent["operation_id"] != prepared_operation["operation_id"]:
        errors.append("intent operation_id does not match the prepared operation")
    if intent["prepared_hash"] != prepared_hash:
        errors.append("intent prepared_hash does not match the canonical prepared operation")
    if intent["logical_action_id"] != logical_action_id(prepared_operation["operation_id"], prepared_hash):
        errors.append("intent logical_action_id is not derived from the prepared operation")
    if intent["target"] != prepared_operation["target"]:
        errors.append("intent target does not match the prepared operation target")
    if intent["parameters"] != prepared_operation["parameters"]["requested"]:
        errors.append("intent parameters are not the prepared requested capacity")
    if intent["expected_current_capacity"] != prepared_operation["current_state"]["capacity"]:
        errors.append("intent expected_current_capacity does not match the prepared current state")
    if prepared_operation["capability"]["capability_version"] != CAPABILITY_VERSION:
        errors.append("prepared operation capability_version is not the capacity-adjustment version")
    if errors:
        raise ExecutionContractError("gamelift-capacity-execution-intent-binding", errors)
