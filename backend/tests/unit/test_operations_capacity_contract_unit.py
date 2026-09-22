"""Contract, hash, and injection tests for additive E2 capacity contracts (#414).

These cover the four additive ``gamelift.capacity-adjustment/1.0`` contracts and
their deterministic helpers. They assert the additive boundary (the capacity
schemas never join the published source-control write-contract set), the
untrusted-input injection guards, the deterministic change/risk/bounds
functions, and the prepared-hash binding that excludes only itself.
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
from operations.contracts import SCHEMA_NAMES, canonical_sha256, load_json, validate_playbook_binding
from operations.contracts.capacity import (
    ADVICE_SCHEMA_NAME,
    AUTHORIZATION_SCHEMA_NAME,
    CAPACITY_SCHEMA_NAMES,
    PREPARED_OPERATION_SCHEMA_NAME,
    PROPOSAL_REQUEST_SCHEMA_NAME,
    CapacityContractError,
    calculate_capacity_risk,
    capacity_bounds_violations,
    capacity_change,
    capacity_prepared_hash,
    effective_authority,
    load_capacity_schema,
    validate_capacity_contract,
    validate_prepared_operation_binding,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

# Genuinely sensitive names that must never appear as a key anywhere in a
# capacity document. The legitimate ``policy`` binding is a version reference,
# not a secret, so it is intentionally excluded.
SENSITIVE_FIELDS = {
    "access_token",
    "account_id",
    "arn",
    "credential",
    "display_name",
    "email",
    "executor_credential",
    "password",
    "provider_response",
    "provider_token",
    "refresh_token",
    "secret",
    "session_token",
}

VALID = {
    PROPOSAL_REQUEST_SCHEMA_NAME: "gamelift-capacity-proposal-request.valid.json",
    ADVICE_SCHEMA_NAME: "gamelift-capacity-advice.valid.json",
    AUTHORIZATION_SCHEMA_NAME: "gamelift-capacity-authorization.valid.json",
    PREPARED_OPERATION_SCHEMA_NAME: "gamelift-capacity-prepared-operation.valid.json",
}


def _fixture(schema_name: str) -> dict[str, Any]:
    return load_json(FIXTURES / VALID[schema_name])


def _all_keys(document: Any) -> set[str]:
    if isinstance(document, dict):
        return set(document).union(*(_all_keys(v) for v in document.values()), set())
    if isinstance(document, list):
        return set().union(*(_all_keys(v) for v in document), set())
    return set()


# -- Schema validity + additive boundary -----------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("schema_name", sorted(CAPACITY_SCHEMA_NAMES))
def test_capacity_schema_is_valid_draft_2020_12(schema_name: str) -> None:
    schema = load_capacity_schema(schema_name)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == f"urn:game-agent:operations:contracts:v1:{schema_name}"


@pytest.mark.unit
def test_capacity_schemas_are_not_in_the_write_contract_set() -> None:
    # Additive: capacity schemas must never join the published write-contract set
    # whose hashes the source-control playbook binds.
    assert CAPACITY_SCHEMA_NAMES.isdisjoint(SCHEMA_NAMES)


@pytest.mark.unit
def test_write_contract_playbook_binding_is_unchanged_by_capacity() -> None:
    prepared = load_json(FIXTURES / "source-control-prepared-operation.valid.json")
    playbook = load_json(FIXTURES / "source-control-playbook.valid.json")
    # Adding the capacity schema files on disk must not change the published
    # write-contract playbook binding.
    validate_playbook_binding(prepared, playbook)


@pytest.mark.unit
@pytest.mark.parametrize("schema_name", sorted(VALID))
def test_valid_fixture_passes_contract(schema_name: str) -> None:
    validate_capacity_contract(schema_name, _fixture(schema_name))


@pytest.mark.unit
@pytest.mark.parametrize("schema_name", sorted(VALID))
def test_no_sensitive_fields_present(schema_name: str) -> None:
    assert _all_keys(_fixture(schema_name)).isdisjoint(SENSITIVE_FIELDS)


# -- Untrusted input: injection + structural absence ------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "injected",
    [
        "requester",
        "principal",
        "policy",
        "calculated_risk",
        "executor_binding",
        "future_executor_binding",
        "credential",
        "approval",
        "current_state",
        "authority_inputs",
        "effective_authority",
        "deployment_mode",
    ],
)
def test_proposal_request_rejects_injected_trusted_field(injected: str) -> None:
    request = _fixture(PROPOSAL_REQUEST_SCHEMA_NAME)
    request[injected] = "attacker-controlled"
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PROPOSAL_REQUEST_SCHEMA_NAME, request)


@pytest.mark.unit
def test_proposal_request_rejects_injected_field_inside_proposal() -> None:
    request = _fixture(PROPOSAL_REQUEST_SCHEMA_NAME)
    request["proposal"]["current"] = {"desired": 1, "minimum": 1, "maximum": 1}
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PROPOSAL_REQUEST_SCHEMA_NAME, request)


@pytest.mark.unit
def test_proposal_request_rejects_wrong_capability_id() -> None:
    request = _fixture(PROPOSAL_REQUEST_SCHEMA_NAME)
    request["capability_id"] = "gamelift.observe-fleet"
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PROPOSAL_REQUEST_SCHEMA_NAME, request)


# -- Deterministic change / risk / bounds -----------------------------------


@pytest.mark.unit
def test_capacity_change_is_requested_minus_current() -> None:
    current = {"desired": 10, "minimum": 2, "maximum": 20}
    requested = {"desired": 14, "minimum": 3, "maximum": 25}
    assert capacity_change(current, requested) == {"desired": 4, "minimum": 1, "maximum": 5}


@pytest.mark.unit
def test_risk_is_deterministic_for_identical_inputs() -> None:
    current = {"desired": 10, "minimum": 2, "maximum": 20}
    requested = {"desired": 14, "minimum": 2, "maximum": 20}
    a = calculate_capacity_risk(current=current, requested=requested, within_bounds=True)
    b = calculate_capacity_risk(current=current, requested=requested, within_bounds=True)
    assert a == b
    assert 0 <= a["score"] <= 100
    assert a["level"] in {"low", "moderate", "high", "critical"}


@pytest.mark.unit
def test_bounds_violation_raises_risk_level() -> None:
    current = {"desired": 10, "minimum": 2, "maximum": 20}
    requested = {"desired": 14, "minimum": 2, "maximum": 20}
    within = calculate_capacity_risk(current=current, requested=requested, within_bounds=True)
    outside = calculate_capacity_risk(current=current, requested=requested, within_bounds=False)
    assert outside["score"] > within["score"]
    assert "bounds-violation" in outside["factors"]


@pytest.mark.unit
def test_bounds_violations_flags_server_owned_limits() -> None:
    limits = {"floor": 1, "ceiling": 30, "max_step": 5}
    requested = {"desired": 40, "minimum": 0, "maximum": 40}
    change = {"desired": 30, "minimum": -2, "maximum": 20}
    violations = capacity_bounds_violations(requested=requested, change=change, limits=limits, target_enrolled=True)
    assert "DESIRED_ABOVE_CEILING" in violations
    assert "MINIMUM_BELOW_FLOOR" in violations
    assert "MAXIMUM_ABOVE_CEILING" in violations
    assert "DELTA_EXCEEDS_MAX_STEP" in violations
    assert violations == sorted(violations)


@pytest.mark.unit
def test_bounds_violations_flags_unenrolled_target() -> None:
    limits = {"floor": 0, "ceiling": 1000, "max_step": 1000}
    requested = {"desired": 5, "minimum": 1, "maximum": 10}
    change = {"desired": 0, "minimum": 0, "maximum": 0}
    violations = capacity_bounds_violations(requested=requested, change=change, limits=limits, target_enrolled=False)
    assert "TARGET_NOT_ENROLLED" in violations


@pytest.mark.unit
def test_effective_authority_is_the_minimum() -> None:
    inputs = {
        "deployment_mode": "operate",
        "tenant_policy": "operate",
        "workspace_policy": "advise",
        "principal_authority": "remediate",
        "capability_maximum": "operate",
        "operation_risk_policy": "operate",
    }
    assert effective_authority(inputs) == "advise"


# -- Prepared hash binding ---------------------------------------------------


@pytest.mark.unit
def test_prepared_hash_excludes_only_itself() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    material = {k: v for k, v in operation.items() if k != "prepared_hash"}
    assert operation["prepared_hash"] == canonical_sha256(material)
    assert capacity_prepared_hash(operation) == operation["prepared_hash"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        ("target", "fleet_id"),
        ("current_state", "observation_hash"),
        ("parameters", "requested", "desired"),
        ("playbook", "playbook_version"),
        ("capability", "capability_version"),
        ("authority", "decision"),
        ("calculated_risk", "score"),
        ("requester", "workspace_id"),
        ("expires_at",),
        ("future_executor_binding", "executor_id"),
        ("required_execution_authority",),
    ],
)
def test_prepared_hash_changes_when_any_bound_field_changes(path: tuple[str, ...]) -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    original = capacity_prepared_hash(operation)
    target: Any = operation
    for part in path[:-1]:
        target = target[part]
    current = target[path[-1]]
    target[path[-1]] = 999999 if isinstance(current, int) else f"{current}-mutated"
    assert capacity_prepared_hash(operation) != original


@pytest.mark.unit
def test_prepared_operation_rejects_tampered_hash() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    operation["target"]["fleet_id"] = "fleet-deadbeef"
    # target changed but hash not recomputed -> semantic failure
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, operation)


@pytest.mark.unit
def test_prepared_operation_rejects_change_that_is_not_the_delta() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    operation["parameters"]["change"]["desired"] += 1
    operation["prepared_hash"] = capacity_prepared_hash(operation)
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, operation)


# -- Authorization semantics -------------------------------------------------


@pytest.mark.unit
def test_authorization_cannot_be_authorized_outright() -> None:
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["decision"] = "authorized"
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authz)


@pytest.mark.unit
def test_authorization_effective_must_be_minimum() -> None:
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["effective_authority"] = "operate"
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authz)


@pytest.mark.unit
def test_disabled_deployment_requires_denied_decision() -> None:
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["authority_inputs"]["deployment_mode"] = "disabled"
    authz["effective_authority"] = "disabled"
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authz)


@pytest.mark.unit
def test_prepared_operation_requires_required_execution_authority() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    del operation["required_execution_authority"]
    operation["prepared_hash"] = capacity_prepared_hash(operation)
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, operation)


@pytest.mark.unit
def test_prepared_operation_rejects_non_remediate_required_execution_authority() -> None:
    # The value is const=remediate in schema and re-checked semantically, so a
    # rehashed operation that lowers it still fails: the advise E2 phase can
    # never elevate — or lower — the execution authority a future E3 re-verifies.
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    operation["required_execution_authority"] = "advise"
    operation["prepared_hash"] = capacity_prepared_hash(operation)
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, operation)


@pytest.mark.unit
def test_non_denied_decision_is_allowed_at_advise_authority() -> None:
    # Regression: the advise-authority E2 phase yields approval_required, not a
    # denial, when effective authority is exactly advise.
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["authority_inputs"] = {
        "deployment_mode": "advise",
        "tenant_policy": "advise",
        "workspace_policy": "advise",
        "principal_authority": "advise",
        "capability_maximum": "advise",
        "operation_risk_policy": "advise",
    }
    authz["effective_authority"] = "advise"
    authz["decision"] = "approval_required"
    authz["reason_codes"] = ["APPROVAL_REQUIRED"]
    validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authz)


@pytest.mark.unit
def test_authorization_requires_remediate_execution_authority() -> None:
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["required_execution_authority"] = "advise"
    with pytest.raises(CapacityContractError):
        validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authz)


@pytest.mark.unit
def test_binding_fails_on_required_execution_authority_mismatch() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    # A valid-looking authorization whose immutable execution authority disagrees
    # with the prepared operation must fail the binding (both are const=remediate
    # so this is defense in depth against a future contract change).
    authz = deepcopy(authz)
    authz["required_execution_authority"] = "operate"
    with pytest.raises(CapacityContractError):
        validate_prepared_operation_binding(operation, authz)


# -- Binding -----------------------------------------------------------------


@pytest.mark.unit
def test_binding_passes_for_matching_documents() -> None:
    validate_prepared_operation_binding(_fixture(PREPARED_OPERATION_SCHEMA_NAME), _fixture(AUTHORIZATION_SCHEMA_NAME))


@pytest.mark.unit
def test_binding_fails_on_hash_mismatch() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["prepared_operation_hash"] = "sha256:" + "e" * 64
    with pytest.raises(CapacityContractError):
        validate_prepared_operation_binding(operation, authz)


@pytest.mark.unit
def test_binding_fails_on_principal_mismatch() -> None:
    operation = _fixture(PREPARED_OPERATION_SCHEMA_NAME)
    authz = _fixture(AUTHORIZATION_SCHEMA_NAME)
    authz["principal"] = dict(authz["principal"], subject_id="subject.someone-else")
    with pytest.raises(CapacityContractError):
        validate_prepared_operation_binding(operation, authz)
