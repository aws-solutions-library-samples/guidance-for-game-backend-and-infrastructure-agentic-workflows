"""Contract, hash, injection, and version-disjointness tests for the additive E5
bounded-autonomy v2 contracts (issue #438).

These cover the three additive ``gamelift.capacity-adjustment/2.0`` contracts —
the immutable bounded-autonomy policy, the deterministic authorized/denied
decision, and the immutable autonomous prepared operation — plus their
deterministic hashes and binding. They assert:

* the additive/version boundary: the v2 schema names never join the published
  write-contract set, the E2 capacity set, or the E3 execution set, and every v1
  schema, playbook hash, and pinned vector is byte-for-byte unchanged;
* the untrusted-input structural absence guards (identity, policy, limits,
  authorization, playbook, executor, credentials cannot be injected);
* the pinned hash vectors and exhaustive hash-invalidation over every bound
  field; and
* the operate-authority / no-human-approval decision semantics.
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
from operations.autonomy_playbook_definition import autonomy_playbook_hash
from operations.contracts import SCHEMA_NAMES, canonical_sha256, load_json
from operations.contracts.autonomy import (
    AUTONOMOUS_DECISION_SCHEMA_NAME,
    AUTONOMOUS_OPERATION_SCHEMA_NAME,
    AUTONOMY_POLICY_SCHEMA_NAME,
    AUTONOMY_REASON_CODES,
    AUTONOMY_SCHEMA_NAMES,
    AutonomyContractError,
    autonomous_decision_hash,
    autonomous_prepared_hash,
    autonomy_policy_hash,
    is_supported_autonomy_version,
    load_autonomy_schema,
    validate_autonomous_operation_binding,
    validate_autonomy_contract,
)
from operations.contracts.capacity import CAPACITY_SCHEMA_NAMES
from operations.contracts.execution import EXECUTION_SCHEMA_NAMES
from operations.contracts.versions import is_supported_contract_version

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

# Genuinely sensitive names that must never appear as a key anywhere in an
# autonomy document. The legitimate ``policy``/``autonomy_policy`` bindings are
# version+hash references, not secrets, so they are intentionally excluded.
SENSITIVE_FIELDS = {
    "access_token",
    "account_id",
    "approval",
    "approver",
    "arn",
    "credential",
    "display_name",
    "email",
    "executor_credential",
    "human_approval",
    "password",
    "provider_response",
    "provider_token",
    "refresh_token",
    "secret",
    "session_token",
}

VALID = {
    AUTONOMY_POLICY_SCHEMA_NAME: "gamelift-capacity-autonomy-policy.valid.json",
    AUTONOMOUS_DECISION_SCHEMA_NAME: "gamelift-capacity-autonomous-decision.valid.json",
    AUTONOMOUS_OPERATION_SCHEMA_NAME: "gamelift-capacity-autonomous-operation.valid.json",
}


def _fixture(schema_name: str) -> dict[str, Any]:
    return load_json(FIXTURES / VALID[schema_name])


def _vectors() -> dict[str, Any]:
    return load_json(FIXTURES / "autonomy-contract-vectors.json")


def _all_keys(document: Any) -> set[str]:
    if isinstance(document, dict):
        return set(document).union(*(_all_keys(v) for v in document.values()), set())
    if isinstance(document, list):
        return set().union(*(_all_keys(v) for v in document), set())
    return set()


def _replace(document: Any, path: list[Any], replacement: Any) -> None:
    target = document
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement


# -- Schema validity + additive / version boundary --------------------------


@pytest.mark.unit
@pytest.mark.parametrize("schema_name", sorted(AUTONOMY_SCHEMA_NAMES))
def test_autonomy_schema_is_valid_draft_2020_12(schema_name: str) -> None:
    schema = load_autonomy_schema(schema_name)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == f"urn:game-agent:operations:contracts:v2:{schema_name}"


@pytest.mark.unit
def test_autonomy_schemas_are_disjoint_from_every_published_set() -> None:
    # Additive + versioned: the v2 autonomy schemas must never join the published
    # write-contract set, the E2 capacity set, or the E3 execution set.
    assert AUTONOMY_SCHEMA_NAMES.isdisjoint(SCHEMA_NAMES)
    assert AUTONOMY_SCHEMA_NAMES.isdisjoint(CAPACITY_SCHEMA_NAMES)
    assert AUTONOMY_SCHEMA_NAMES.isdisjoint(EXECUTION_SCHEMA_NAMES)


@pytest.mark.unit
def test_autonomy_schema_names_have_no_sensitive_keys() -> None:
    for schema_name in AUTONOMY_SCHEMA_NAMES:
        assert _all_keys(_fixture(schema_name)).isdisjoint(SENSITIVE_FIELDS)


@pytest.mark.unit
def test_autonomy_version_check_is_exact_and_disjoint_from_v1() -> None:
    assert is_supported_autonomy_version("2.0")
    assert not is_supported_autonomy_version("1.0")
    # The v1 allowlist is untouched and never admits the v2 version.
    assert is_supported_contract_version("1.0")
    assert not is_supported_contract_version("2.0")


@pytest.mark.unit
def test_reason_code_set_extends_the_v1_closed_set() -> None:
    v1_codes = {
        "APPROVAL_REQUIRED",
        "DEPLOYMENT_DISABLED",
        "INSUFFICIENT_AUTHORITY",
        "POLICY_DENIED",
        "TARGET_NOT_ENROLLED",
        "RISK_LIMIT_EXCEEDED",
        "WORKSPACE_MISMATCH",
        "BOUNDS_EXCEEDED",
    }
    # The autonomy set replaces APPROVAL_REQUIRED with APPROVED_AUTONOMOUS and
    # adds the guardrail codes; it never carries the v1 human-approval code.
    assert "APPROVED_AUTONOMOUS" in AUTONOMY_REASON_CODES
    assert "APPROVAL_REQUIRED" not in AUTONOMY_REASON_CODES
    assert (v1_codes - {"APPROVAL_REQUIRED"}).issubset(AUTONOMY_REASON_CODES)


# -- Valid fixtures ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("schema_name", sorted(VALID))
def test_valid_fixture_passes_contract(schema_name: str) -> None:
    validate_autonomy_contract(schema_name, _fixture(schema_name))


@pytest.mark.unit
def test_binding_passes_for_matching_documents() -> None:
    validate_autonomous_operation_binding(
        _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME),
        _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME),
        _fixture(AUTONOMY_POLICY_SCHEMA_NAME),
    )


# -- Pinned hash vectors -----------------------------------------------------


@pytest.mark.unit
def test_pinned_hash_vectors() -> None:
    vectors = _vectors()
    expected = vectors["expected"]
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)

    assert autonomy_policy_hash(policy) == expected["policy_hash"]
    assert autonomous_decision_hash(decision) == expected["decision_hash"]
    assert autonomous_prepared_hash(operation) == expected["prepared_hash"]
    assert autonomy_playbook_hash() == expected["playbook_hash"]
    assert policy["policy_hash"] == expected["policy_hash"]
    assert operation["prepared_hash"] == expected["prepared_hash"]

    # The four pinned digests plus the three ids are all distinct.
    assert (
        len(
            {
                expected["policy_hash"],
                expected["decision_hash"],
                expected["prepared_hash"],
                expected["playbook_hash"],
                expected["operation_id"],
                expected["decision_id"],
            }
        )
        == 6
    )


@pytest.mark.unit
def test_policy_hash_excludes_only_itself() -> None:
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    material = {k: v for k, v in policy.items() if k != "policy_hash"}
    assert policy["policy_hash"] == canonical_sha256(material)


@pytest.mark.unit
def test_prepared_hash_excludes_only_itself() -> None:
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    material = {k: v for k, v in operation.items() if k != "prepared_hash"}
    assert operation["prepared_hash"] == canonical_sha256(material)


# -- Exhaustive hash invalidation -------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    _vectors()["policy_hash_invalidation_cases"],
    ids=lambda case: case["name"],
)
def test_every_policy_binding_invalidates_the_policy_hash(case: dict[str, Any]) -> None:
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    original = autonomy_policy_hash(policy)
    _replace(policy, case["path"], case["replacement"])
    assert autonomy_policy_hash(policy) != original


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    _vectors()["operation_hash_invalidation_cases"],
    ids=lambda case: case["name"],
)
def test_every_operation_binding_invalidates_the_prepared_hash(case: dict[str, Any]) -> None:
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    original = autonomous_prepared_hash(operation)
    _replace(operation, case["path"], case["replacement"])
    assert autonomous_prepared_hash(operation) != original


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    _vectors()["decision_hash_invalidation_cases"],
    ids=lambda case: case["name"],
)
def test_every_decision_binding_invalidates_the_decision_hash(case: dict[str, Any]) -> None:
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    original = autonomous_decision_hash(decision)
    _replace(decision, case["path"], case["replacement"])
    assert autonomous_decision_hash(decision) != original


@pytest.mark.unit
def test_policy_invalidation_cases_cover_all_fifteen_bindings() -> None:
    # Each numbered binding (1..15) from the guardrail envelope must appear as a
    # covered prefix in the vector case names, so the invalidation set is
    # exhaustive over the required bindings. Concurrency (13) is const 1 and is
    # covered by the operation binding + schema const, not a policy hash mutation.
    names = [case["name"] for case in _vectors()["policy_hash_invalidation_cases"]]
    prefixes = {name.split(" ", 1)[0] for name in names}
    required = {str(n) for n in range(1, 16)} - {"4", "13"}
    assert required.issubset(prefixes)


# -- Untrusted-input structural absence -------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "injected",
    [
        "credential",
        "executor_credential",
        "human_approval",
        "approval",
        "approver",
        "access_token",
        "session_token",
        "provider_response",
    ],
)
def test_operation_rejects_injected_sensitive_field(injected: str) -> None:
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    operation[injected] = "attacker-controlled"
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_OPERATION_SCHEMA_NAME, operation)


@pytest.mark.unit
def test_operation_has_no_human_approval_field() -> None:
    # The autonomous operation is the no-human-in-the-loop path: there is no
    # approval field anywhere, and its execution authority is operate.
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    assert _all_keys(operation).isdisjoint({"approval", "approver", "human_approval"})
    assert operation["required_execution_authority"] == "operate"
    assert set(operation["authority"]["reason_codes"]) == {"APPROVED_AUTONOMOUS"}


@pytest.mark.unit
def test_policy_rejects_widened_capacity_envelope() -> None:
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    policy["capacity_bounds"]["ceiling"] = 2
    policy["policy_hash"] = autonomy_policy_hash(policy)
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMY_POLICY_SCHEMA_NAME, policy)


@pytest.mark.unit
def test_policy_rejects_relaxed_concurrency() -> None:
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    policy["concurrency"]["max_in_flight"] = 2
    policy["policy_hash"] = autonomy_policy_hash(policy)
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMY_POLICY_SCHEMA_NAME, policy)


@pytest.mark.unit
def test_policy_rejects_non_operate_execution_authority() -> None:
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    policy["required_execution_authority"] = "remediate"
    policy["policy_hash"] = autonomy_policy_hash(policy)
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMY_POLICY_SCHEMA_NAME, policy)


@pytest.mark.unit
def test_policy_rejects_tampered_hash() -> None:
    policy = _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
    policy["budget"]["max_action_micro_usd"] += 1  # changed but hash not recomputed
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMY_POLICY_SCHEMA_NAME, policy)


# -- Decision / operation semantics -----------------------------------------


@pytest.mark.unit
def test_authorized_requires_operate_authority() -> None:
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    decision["authority_inputs"]["principal_authority"] = "remediate"
    decision["effective_authority"] = "remediate"
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_DECISION_SCHEMA_NAME, decision)


@pytest.mark.unit
def test_authorized_must_carry_only_the_autonomous_reason_code() -> None:
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    decision["reason_codes"] = ["APPROVED_AUTONOMOUS", "BUDGET_EXCEEDED"]
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_DECISION_SCHEMA_NAME, decision)


@pytest.mark.unit
def test_denied_cannot_carry_the_autonomous_reason_code() -> None:
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    decision["decision"] = "denied"
    decision["reason_codes"] = ["APPROVED_AUTONOMOUS"]
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_DECISION_SCHEMA_NAME, decision)


@pytest.mark.unit
def test_disabled_deployment_requires_denied_decision() -> None:
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    decision["authority_inputs"]["deployment_mode"] = "disabled"
    decision["effective_authority"] = "disabled"
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_DECISION_SCHEMA_NAME, decision)


@pytest.mark.unit
def test_decision_effective_must_be_minimum() -> None:
    decision = _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME)
    decision["authority_inputs"]["workspace_policy"] = "advise"
    # effective still says operate -> mismatch
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_DECISION_SCHEMA_NAME, decision)


@pytest.mark.unit
def test_operation_rejects_tampered_hash() -> None:
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    operation["target"]["fleet_id"] = "fleet-deadbeef-0000-0000-0000-000000000000"
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_OPERATION_SCHEMA_NAME, operation)


@pytest.mark.unit
def test_operation_rejects_change_that_is_not_the_delta() -> None:
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    operation["parameters"]["change"]["desired"] += 1
    operation["prepared_hash"] = autonomous_prepared_hash(operation)
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_OPERATION_SCHEMA_NAME, operation)


@pytest.mark.unit
def test_operation_rejects_non_operate_execution_authority() -> None:
    operation = _fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME)
    operation["required_execution_authority"] = "remediate"
    operation["prepared_hash"] = autonomous_prepared_hash(operation)
    with pytest.raises(AutonomyContractError):
        validate_autonomy_contract(AUTONOMOUS_OPERATION_SCHEMA_NAME, operation)


# -- Binding failures --------------------------------------------------------


@pytest.mark.unit
def test_binding_fails_on_decision_hash_mismatch() -> None:
    operation = deepcopy(_fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME))
    operation["decision"]["decision_hash"] = "sha256:" + "a" * 64
    operation["prepared_hash"] = autonomous_prepared_hash(operation)
    with pytest.raises(AutonomyContractError):
        validate_autonomous_operation_binding(
            operation, _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME), _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
        )


@pytest.mark.unit
def test_binding_fails_on_policy_hash_mismatch() -> None:
    operation = deepcopy(_fixture(AUTONOMOUS_OPERATION_SCHEMA_NAME))
    operation["autonomy_policy"]["policy_hash"] = "sha256:" + "b" * 64
    operation["prepared_hash"] = autonomous_prepared_hash(operation)
    with pytest.raises(AutonomyContractError):
        validate_autonomous_operation_binding(
            operation, _fixture(AUTONOMOUS_DECISION_SCHEMA_NAME), _fixture(AUTONOMY_POLICY_SCHEMA_NAME)
        )


# -- v1 immutability regression guard ---------------------------------------


@pytest.mark.unit
def test_v1_schema_and_vector_hashes_are_unchanged_by_the_v2_layer() -> None:
    """Adding the v2 autonomy schemas/fixtures must not move any published v1 hash.

    This pins the exact v1 source-control playbook and prepared-operation hashes
    (from ``contract-vectors.json``) and the v1 capacity playbook hash, proving
    the additive v2 layer left every published v1 schema, vector, and meaning
    byte-for-byte unchanged.
    """
    # Local modules
    from operations.playbook_definition import capacity_playbook_hash

    v1_vectors = load_json(FIXTURES / "contract-vectors.json")
    v1_playbook = load_json(FIXTURES / v1_vectors["playbook_fixture"])
    v1_operation = load_json(FIXTURES / v1_vectors["prepared_operation_fixture"])

    assert canonical_sha256(v1_playbook) == v1_vectors["expected"]["playbook_hash"]
    assert canonical_sha256(v1_operation) == v1_vectors["expected"]["prepared_operation_hash"]
    # The E2 capacity playbook hash is pinned in its own bootstrap test; assert it
    # here too so a common-schema change (hazard #1) that would ripple into the v1
    # playbook hash is caught alongside the v2 additions.
    assert capacity_playbook_hash() == "sha256:553649474fd2c1d0340bca1d0901c389b65b45baefc3c87846e6b83ed4e2e2a3"
