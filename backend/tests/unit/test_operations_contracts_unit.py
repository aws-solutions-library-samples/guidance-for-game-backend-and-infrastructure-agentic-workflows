"""Contract, compatibility, and deterministic hash tests for operations v1."""

from __future__ import annotations

# Standard library
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

# Third-party packages
import pytest
from jsonschema import Draft202012Validator

# Local modules
from operations.contracts import (
    CONTRACT_VERSION,
    OPERATION_STATES,
    SCHEMA_NAMES,
    CanonicalizationError,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    is_supported_contract_version,
    load_json,
    load_schema,
    source_control_branch_name,
    source_control_content_hash,
    validate_approval_binding,
    validate_authorization_binding,
    validate_contract,
    validate_playbook_binding,
    validate_state_transition,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

VALID_FIXTURES = {
    "application-error.valid.json": "application-error",
    "approval-record.valid.json": "approval-record",
    "authorization-decision.valid.json": "authorization-decision",
    "ledger-event.valid.json": "ledger-event",
    "operation-state-change.valid.json": "operation-state-change",
    "prepare-operation-request.valid.json": "prepare-operation-request",
    "source-control-playbook.valid.json": "playbook",
    "source-control-prepared-operation.valid.json": "source-control-prepared-operation",
}

SENSITIVE_PROVIDER_FIELDS = {
    "access_token",
    "display_name",
    "email",
    "provider_response",
    "provider_token",
    "refresh_token",
}


def _fixture(filename: str) -> Any:
    return load_json(FIXTURES / filename)


def _replace(document: Any, path: list[str | int], replacement: Any) -> None:
    target = document
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement


def _all_keys(document: Any) -> set[str]:
    if isinstance(document, dict):
        return set(document).union(*(_all_keys(value) for value in document.values()), set())
    if isinstance(document, list):
        return set().union(*(_all_keys(value) for value in document), set())
    return set()


def _set_inline_files(operation: dict[str, Any], files: list[tuple[str, str]]) -> None:
    operation["parameters"]["files"] = [
        {
            "path": path,
            "content_encoding": "utf-8",
            "content": content,
            "content_hash": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
        }
        for path, content in files
    ]
    operation["duplicate_content_hash"] = source_control_content_hash(operation)


def test_all_published_schemas_are_valid_draft_2020_12() -> None:
    assert SCHEMA_NAMES
    for schema_name in SCHEMA_NAMES:
        schema = load_schema(schema_name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].startswith("urn:game-agent:operations:contracts:v1:")
        Draft202012Validator.check_schema(schema)


def test_loaded_schemas_are_defensive_copies() -> None:
    schema = load_schema("application-error")
    schema["required"].clear()
    schema["additionalProperties"] = True

    assert load_schema("application-error")["required"]
    with pytest.raises(ContractValidationError):
        validate_contract("application-error", {})


@pytest.mark.parametrize(("filename", "schema_name"), VALID_FIXTURES.items())
def test_valid_contract_fixtures(filename: str, schema_name: str) -> None:
    validate_contract(schema_name, _fixture(filename))


def test_rfc_8785_canonicalization_vector() -> None:
    document = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "literals": [None, True, False],
    }

    assert canonicalize(document) == (
        b'{"literals":[null,true,false],"numbers":' b"[333333333.3333333,1e+30,4.5,0.002,1e-27]}"
    )


def test_json_loader_and_contract_validation_reject_non_i_json_input() -> None:
    with pytest.raises(CanonicalizationError, match="duplicate"):
        load_json('{"same": 1, "same": 2}')
    with pytest.raises(CanonicalizationError, match="non-finite"):
        load_json('{"number": NaN}')
    with pytest.raises(CanonicalizationError):
        load_json(f'{{"number": {2**60}}}')
    with pytest.raises(CanonicalizationError):
        load_json('"\\ud800"')
    with pytest.raises(CanonicalizationError):
        canonicalize({"number": 2**60})
    with pytest.raises(CanonicalizationError):
        load_json("[" * 2000 + "0" + "]" * 2000)

    event = _fixture("ledger-event.valid.json")
    event["sequence"] = 2**60
    with pytest.raises(ContractValidationError, match="canonical I-JSON domain"):
        validate_contract("ledger-event", event)

    nested: Any = 0
    for _ in range(2000):
        nested = [nested]
    with pytest.raises(ContractValidationError, match="canonical I-JSON domain"):
        validate_contract("application-error", nested)


def test_fixed_playbook_and_prepared_operation_hash_vectors() -> None:
    vectors = _fixture("contract-vectors.json")
    playbook = _fixture(vectors["playbook_fixture"])
    operation = _fixture(vectors["prepared_operation_fixture"])
    expected = vectors["expected"]

    assert canonical_sha256(playbook) == expected["playbook_hash"]
    assert canonical_sha256(operation) == expected["prepared_operation_hash"]
    assert source_control_content_hash(operation) == expected["duplicate_content_hash"]
    assert source_control_branch_name(operation["operation_id"]) == expected["proposal_branch"]

    assert operation["operation_id"] == expected["operation_id"]
    assert operation["idempotency_token"] == expected["idempotency_token"]
    assert operation["operation_contract_version"] == expected["operation_contract_version"]
    assert (
        len(
            {
                expected["operation_id"],
                expected["prepared_operation_hash"],
                expected["idempotency_token"],
                expected["operation_contract_version"],
            }
        )
        == 4
    )


def test_playbook_binds_the_complete_immutable_schema_set() -> None:
    playbook = _fixture("source-control-playbook.valid.json")
    actual = {binding["schema_id"]: binding["schema_hash"] for binding in playbook["schemas"]}
    expected = {
        load_schema(name)["$id"]: canonical_sha256(load_schema(name)) for name in SCHEMA_NAMES if name != "playbook"
    }
    assert actual == expected

    invalid_authority = deepcopy(playbook)
    invalid_authority["authority"]["maximum_mode"] = "observe"
    with pytest.raises(ContractValidationError, match="minimum_mode cannot exceed maximum_mode"):
        validate_contract("playbook", invalid_authority)


@pytest.mark.parametrize(
    "case",
    _fixture("contract-vectors.json")["hash_invalidation_cases"],
    ids=lambda case: case["name"],
)
def test_source_control_security_inputs_invalidate_operation_hash(case: dict[str, Any]) -> None:
    operation = _fixture("source-control-prepared-operation.valid.json")
    original_hash = canonical_sha256(operation)
    _replace(operation, case["path"], case["replacement"])

    assert canonical_sha256(operation) != original_hash


@pytest.mark.parametrize(
    "case",
    _fixture("contract-vectors.json")["invalid_contract_cases"],
    ids=lambda case: case["name"],
)
def test_invalid_contract_vectors_fail_closed(case: dict[str, Any]) -> None:
    document = _fixture(case["fixture"])
    _replace(document, case["path"], case["replacement"])

    with pytest.raises(ContractValidationError):
        validate_contract(case["schema"], document)


def test_operation_request_body_rejects_every_principal_field() -> None:
    request = _fixture("prepare-operation-request.valid.json")
    identity_fields = {
        "requester",
        "approver",
        "subject_id",
        "client_id",
        "tenant_id",
        "workspace_id",
        "email",
        "display_name",
    }

    for field in identity_fields:
        injected = deepcopy(request)
        injected[field] = "untrusted"
        with pytest.raises(ContractValidationError):
            validate_contract("prepare-operation-request", injected)


def test_sensitive_provider_fields_are_excluded_from_stored_contracts() -> None:
    for filename in VALID_FIXTURES:
        assert not (_all_keys(_fixture(filename)) & SENSITIVE_PROVIDER_FIELDS)

    operation = _fixture("source-control-prepared-operation.valid.json")
    for field in SENSITIVE_PROVIDER_FIELDS:
        injected = deepcopy(operation)
        injected[field] = "untrusted"
        with pytest.raises(ContractValidationError):
            validate_contract("source-control-prepared-operation", injected)


def test_playbook_and_approval_bind_to_exact_stored_content() -> None:
    playbook = _fixture("source-control-playbook.valid.json")
    operation = _fixture("source-control-prepared-operation.valid.json")
    approval = _fixture("approval-record.valid.json")

    validate_playbook_binding(operation, playbook)
    validate_approval_binding(approval, operation)

    changed_operation = deepcopy(operation)
    changed_operation["policy"]["policy_version"] = "13"
    with pytest.raises(ContractValidationError, match="approval hash"):
        validate_approval_binding(approval, changed_operation)

    denied = deepcopy(approval)
    denied["decision"] = "denied"
    with pytest.raises(ContractValidationError, match="decision is not granted"):
        validate_approval_binding(denied, operation)

    wrong_policy = deepcopy(approval)
    wrong_policy["policy_version"] = "13"
    with pytest.raises(ContractValidationError, match="policy_version"):
        validate_approval_binding(wrong_policy, operation)

    changed_playbook = deepcopy(playbook)
    changed_playbook["retry_policy"]["max_attempts"] = 4
    with pytest.raises(ContractValidationError, match="playbook_hash"):
        validate_playbook_binding(operation, changed_playbook)


def test_binding_helpers_reject_profile_invalid_operations() -> None:
    playbook = _fixture("source-control-playbook.valid.json")
    operation = _fixture("source-control-prepared-operation.valid.json")
    approval = _fixture("approval-record.valid.json")

    operation["parameters"]["files"][0]["path"] = "../outside.yaml"
    operation["duplicate_content_hash"] = source_control_content_hash(operation)
    approval["prepared_operation_hash"] = canonical_sha256(operation)

    with pytest.raises(ContractValidationError, match="normalized relative POSIX path"):
        validate_playbook_binding(operation, playbook)
    with pytest.raises(ContractValidationError, match="normalized relative POSIX path"):
        validate_approval_binding(approval, operation)


def test_playbook_binding_enforces_all_inline_file_limits() -> None:
    playbook = _fixture("source-control-playbook.valid.json")

    operation = _fixture("source-control-prepared-operation.valid.json")
    _set_inline_files(operation, [("infrastructure/game.exe", "safe")])
    with pytest.raises(ContractValidationError, match="extension is not allowed"):
        validate_playbook_binding(operation, playbook)

    operation = _fixture("source-control-prepared-operation.valid.json")
    _set_inline_files(operation, [("infrastructure/game.yaml", "x" * 250001)])
    with pytest.raises(ContractValidationError, match="max_file_bytes"):
        validate_playbook_binding(operation, playbook)

    operation = _fixture("source-control-prepared-operation.valid.json")
    _set_inline_files(operation, [(f"infrastructure/game-{index}.yaml", "safe") for index in range(26)])
    with pytest.raises(ContractValidationError, match="max_files"):
        validate_playbook_binding(operation, playbook)

    operation = _fixture("source-control-prepared-operation.valid.json")
    _set_inline_files(operation, [(f"infrastructure/game-{index}.yaml", "x" * 200001) for index in range(5)])
    with pytest.raises(ContractValidationError, match="max_total_bytes"):
        validate_playbook_binding(operation, playbook)


def test_playbook_binding_resolves_and_hashes_referenced_content() -> None:
    playbook = _fixture("source-control-playbook.valid.json")
    operation = _fixture("source-control-prepared-operation.valid.json")
    inline_file = operation["parameters"]["files"][0]
    content = inline_file["content"].encode("utf-8")
    operation["parameters"]["files"][0] = {
        "path": inline_file["path"],
        "content_ref": "content:game-template-v1",
        "content_hash": inline_file["content_hash"],
    }
    operation["duplicate_content_hash"] = source_control_content_hash(operation)

    with pytest.raises(ContractValidationError, match="without a content resolver"):
        validate_playbook_binding(operation, playbook)

    validate_playbook_binding(operation, playbook, content_resolver=lambda _: content)
    with pytest.raises(ContractValidationError, match="does not match its bound hash"):
        validate_playbook_binding(operation, playbook, content_resolver=lambda _: b"different")


def test_playbook_binding_resolves_hashes_and_bounds_referenced_diff() -> None:
    playbook = _fixture("source-control-playbook.valid.json")
    operation = _fixture("source-control-prepared-operation.valid.json")
    rendered_diff = operation["parameters"]["diff"]["rendered_diff"].encode("utf-8")
    operation["parameters"]["diff"] = {
        "diff_ref": "content:rendered-diff-v1",
        "diff_hash": operation["parameters"]["diff"]["diff_hash"],
    }
    operation["duplicate_content_hash"] = source_control_content_hash(operation)

    with pytest.raises(ContractValidationError, match="diff_ref cannot be verified"):
        validate_playbook_binding(operation, playbook)

    validate_playbook_binding(operation, playbook, content_resolver=lambda _: rendered_diff)
    with pytest.raises(ContractValidationError, match="does not match its bound hash"):
        validate_playbook_binding(operation, playbook, content_resolver=lambda _: b"different")

    oversized_diff = b"x" * 5_000_001
    operation["parameters"]["diff"]["diff_hash"] = f"sha256:{hashlib.sha256(oversized_diff).hexdigest()}"
    operation["duplicate_content_hash"] = source_control_content_hash(operation)
    with pytest.raises(ContractValidationError, match="max rendered diff bytes"):
        validate_playbook_binding(operation, playbook, content_resolver=lambda _: oversized_diff)


def test_duplicate_content_does_not_combine_independent_workflows() -> None:
    first = _fixture("source-control-prepared-operation.valid.json")
    second = deepcopy(first)
    second["operation_id"] = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
    second["idempotency_token"] = "idem_BBBBBBBBBBBBBBBBBBBB"
    second["target"]["proposal_branch"] = source_control_branch_name(second["operation_id"])

    validate_contract("source-control-prepared-operation", second)
    assert source_control_content_hash(second) == source_control_content_hash(first)
    assert canonical_sha256(second) != canonical_sha256(first)
    assert second["target"]["proposal_branch"] != first["target"]["proposal_branch"]


def test_authorization_effective_authority_is_deterministic_minimum() -> None:
    decision = _fixture("authorization-decision.valid.json")
    validate_contract("authorization-decision", decision)

    decision["effective_authority"] = "operate"
    with pytest.raises(ContractValidationError, match="lowest authority"):
        validate_contract("authorization-decision", decision)


def test_authorization_decisions_fail_closed_and_bind_playbook_approval() -> None:
    playbook = _fixture("source-control-playbook.valid.json")
    operation = _fixture("source-control-prepared-operation.valid.json")
    decision = _fixture("authorization-decision.valid.json")

    validate_authorization_binding(decision, operation, playbook)

    disabled = deepcopy(decision)
    disabled["authority_inputs"]["deployment_mode"] = "disabled"
    disabled["effective_authority"] = "disabled"
    disabled["decision"] = "authorized"
    disabled["reason_codes"] = ["AUTHORIZED"]
    with pytest.raises(ContractValidationError, match="disabled deployment mode"):
        validate_contract("authorization-decision", disabled)

    insufficient = deepcopy(decision)
    insufficient["authority_inputs"]["principal_authority"] = "advise"
    insufficient["effective_authority"] = "advise"
    with pytest.raises(ContractValidationError, match="requires at least remediate"):
        validate_contract("authorization-decision", insufficient)

    excessive = deepcopy(decision)
    excessive["authority_inputs"] = {key: "operate" for key in excessive["authority_inputs"]}
    excessive["effective_authority"] = "operate"
    validate_contract("authorization-decision", excessive)
    with pytest.raises(ContractValidationError, match="exceeds the playbook maximum"):
        validate_authorization_binding(excessive, operation, playbook)

    bypass = deepcopy(decision)
    bypass["decision"] = "authorized"
    bypass["reason_codes"] = ["AUTHORIZED"]
    validate_contract("authorization-decision", bypass)
    with pytest.raises(ContractValidationError, match="cannot bypass"):
        validate_authorization_binding(bypass, operation, playbook)


def test_operation_lifecycle_schema_and_transition_table_match() -> None:
    schema_states = set(load_schema("common")["$defs"]["operation_state"]["enum"])
    assert schema_states == OPERATION_STATES

    validate_state_transition(None, "prepared")
    validate_state_transition("pending_approval", "approved")
    validate_state_transition("executing", "retry_pending")
    validate_state_transition("executing", "succeeded")

    with pytest.raises(ValueError):
        validate_state_transition("succeeded", "executing")
    with pytest.raises(ValueError):
        validate_state_transition("prepared", "dispatched")

    state_change = _fixture("operation-state-change.valid.json")
    state_change["reason_code"] = "EXECUTION_SUCCEEDED"
    with pytest.raises(ContractValidationError, match="OPERATION_PREPARED"):
        validate_contract("operation-state-change", state_change)

    state_change["previous_state"] = "prepared"
    state_change["new_state"] = "dispatched"
    state_change["reason_code"] = "DISPATCHED"
    with pytest.raises(ContractValidationError, match="not allowed"):
        validate_contract("operation-state-change", state_change)


def test_ledger_event_type_must_match_typed_payload() -> None:
    event = _fixture("ledger-event.valid.json")
    event["event_type"] = "provider.result-recorded"

    with pytest.raises(ContractValidationError):
        validate_contract("ledger-event", event)


def test_version_compatibility_is_exact_and_fail_closed() -> None:
    assert CONTRACT_VERSION == "1.0"
    assert is_supported_contract_version("1.0")
    assert not is_supported_contract_version("1.1")
    assert not is_supported_contract_version("2.0")
