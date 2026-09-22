"""Contract, hash, and injection tests for additive E3 execution contracts (#415).

These cover the three additive ``gamelift.capacity-adjustment/1.0`` *execution*
contracts and their deterministic helpers:

* ``gamelift-capacity-execution-intent`` — the server-owned provider-write
  intent. It carries ONLY the exact ``UpdateFleetCapacity`` parameters
  (fleet id, location, desired/min/max), the prepared operation id, the bound
  ``prepared_hash`` it derives from, the hash-bound expected current state, and
  a stable, attempt-independent ``logical_action_id``. It holds no identity,
  credential, provider response, ARN, account id, or executable content, and
  ``additionalProperties`` is false everywhere.
* ``gamelift-capacity-execution-result`` — the normalized, bounded outcome of
  the single provider write (or reconciliation). It never carries an ARN,
  account id, provider token, or raw provider response.
* ``gamelift-capacity-execution-verification`` — the bounded post-action
  ``DescribeFleetCapacity`` verification of observed capacity against the
  hash-bound expected target.

Like the E2 capacity layer, this layer is **additive**: it reuses the immutable
``common`` ``$defs`` but never joins the published write-contract
``SCHEMA_NAMES`` set, so it cannot change a published v1 write contract or its
playbook hash.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from pathlib import Path
from typing import Any

# Third-party packages
import pytest
from jsonschema import Draft202012Validator

# Local modules
from operations.contracts import SCHEMA_NAMES, canonical_sha256, load_json
from operations.contracts.capacity import capacity_prepared_hash
from operations.contracts.execution import (
    EXECUTION_INTENT_SCHEMA_NAME,
    EXECUTION_RESULT_SCHEMA_NAME,
    EXECUTION_SCHEMA_NAMES,
    EXECUTION_VERIFICATION_SCHEMA_NAME,
    ExecutionContractError,
    build_execution_intent,
    execution_intent_hash,
    load_execution_schema,
    validate_execution_contract,
    validate_execution_intent_binding,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

SENSITIVE_FIELDS = {
    "access_token",
    "account_id",
    "arn",
    "credential",
    "email",
    "executor_credential",
    "fleet_arn",
    "password",
    "provider_response",
    "provider_token",
    "refresh_token",
    "secret",
    "session_token",
}

VALID = {
    EXECUTION_INTENT_SCHEMA_NAME: "gamelift-capacity-execution-intent.valid.json",
    EXECUTION_RESULT_SCHEMA_NAME: "gamelift-capacity-execution-result.valid.json",
    EXECUTION_VERIFICATION_SCHEMA_NAME: "gamelift-capacity-execution-verification.valid.json",
}


def _fixture(schema_name: str) -> dict[str, Any]:
    return load_json(FIXTURES / VALID[schema_name])


def _prepared() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")


def _all_keys(document: Any) -> set[str]:
    if isinstance(document, dict):
        return set(document).union(*(_all_keys(v) for v in document.values()), set())
    if isinstance(document, list):
        return set().union(*(_all_keys(v) for v in document), set())
    return set()


# -- Additive boundary ------------------------------------------------------


def test_execution_schema_names_are_disjoint_from_write_contract_set() -> None:
    assert EXECUTION_SCHEMA_NAMES.isdisjoint(SCHEMA_NAMES)


@pytest.mark.parametrize("schema_name", sorted(EXECUTION_SCHEMA_NAMES))
def test_execution_schema_is_valid_draft_2020_12(schema_name: str) -> None:
    schema = load_execution_schema(schema_name)
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("schema_name", sorted(EXECUTION_SCHEMA_NAMES))
def test_valid_fixture_passes_contract(schema_name: str) -> None:
    validate_execution_contract(schema_name, _fixture(schema_name))


@pytest.mark.parametrize("schema_name", sorted(EXECUTION_SCHEMA_NAMES))
def test_no_sensitive_field_names_anywhere(schema_name: str) -> None:
    assert _all_keys(_fixture(schema_name)).isdisjoint(SENSITIVE_FIELDS)


# -- Intent build + hash binding --------------------------------------------


def test_build_execution_intent_binds_prepared_operation() -> None:
    prepared = _prepared()
    intent = build_execution_intent(prepared)
    validate_execution_contract(EXECUTION_INTENT_SCHEMA_NAME, intent)

    assert intent["operation_id"] == prepared["operation_id"]
    assert intent["prepared_hash"] == capacity_prepared_hash(prepared)
    # The write parameters are exactly the prepared requested capacity.
    assert intent["parameters"] == prepared["parameters"]["requested"]
    # The hash-bound expected state equals the prepared current state.
    assert intent["expected_current_capacity"] == prepared["current_state"]["capacity"]
    assert intent["target"] == prepared["target"]


def test_execution_intent_is_deterministic_and_attempt_independent() -> None:
    prepared = _prepared()
    first = build_execution_intent(prepared)
    second = build_execution_intent(prepared)
    # No attempt counter, uuid, or wall-clock leaks into the intent: two builds
    # from the same prepared operation are byte-for-byte identical.
    assert first == second
    assert execution_intent_hash(first) == execution_intent_hash(second)
    # logical_action_id is stable and independent of any attempt.
    assert first["logical_action_id"] == second["logical_action_id"]


def test_execution_intent_binding_detects_prepared_hash_tamper() -> None:
    prepared = _prepared()
    intent = build_execution_intent(prepared)
    validate_execution_intent_binding(intent, prepared)

    tampered = deepcopy(prepared)
    tampered["parameters"]["requested"]["desired"] += 1
    with pytest.raises(ExecutionContractError):
        validate_execution_intent_binding(intent, tampered)


def test_execution_intent_binding_detects_operation_id_mismatch() -> None:
    prepared = _prepared()
    intent = build_execution_intent(prepared)
    intent = deepcopy(intent)
    intent["operation_id"] = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
    with pytest.raises(ExecutionContractError):
        validate_execution_intent_binding(intent, prepared)


def test_execution_intent_rejects_injected_field() -> None:
    prepared = _prepared()
    intent = build_execution_intent(prepared)
    intent = deepcopy(intent)
    intent["provider_token"] = "nope"
    with pytest.raises(ExecutionContractError):
        validate_execution_contract(EXECUTION_INTENT_SCHEMA_NAME, intent)


def test_load_unknown_execution_schema_rejected() -> None:
    with pytest.raises(ValueError):
        load_execution_schema("not-a-real-execution-schema")
