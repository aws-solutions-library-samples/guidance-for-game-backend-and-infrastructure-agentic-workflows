"""Contract, hash, cursor, and mutation tests for the additive E4 control plane (#416).

These cover the eight additive control-plane contracts and their helpers. They
assert the additive boundary (the E4 schemas never join the published
source-control / capacity / execution registries), the closed-object /
no-unknown-field guards, the semantic invariants (kill-switch freshness and
phase ordering, discovery availability lattice, list bounds, projection
visibility, control compare-and-set, immutable audit-record hash binding), the
public-safe field exclusions, and the opaque cursor's tamper rejection.

Every contract is exercised red-green: a valid fixture must pass, and a targeted
mutation of each invariant must fail.
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
from operations.contracts import (
    CONTROL_SCHEMA_NAMES,
    EXECUTION_SCHEMA_NAMES,
    SCHEMA_NAMES,
    canonical_sha256,
    load_json,
)
from operations.contracts.capacity import CAPACITY_SCHEMA_NAMES
from operations.contracts.control_plane import (
    CAPABILITY_DISCOVERY_SCHEMA_NAME,
    CAPABILITY_ID,
    CONTROL_AUDIT_RECORD_SCHEMA_NAME,
    CONTROL_REQUEST_SCHEMA_NAME,
    CONTROL_RESPONSE_SCHEMA_NAME,
    DETAIL_PROJECTION_SCHEMA_NAME,
    KILL_SWITCH_SCHEMA_NAME,
    LIST_REQUEST_SCHEMA_NAME,
    LIST_RESPONSE_SCHEMA_NAME,
    MAX_PAGE_SIZE,
    ROUTE_KEYS,
    ControlContractError,
    CursorError,
    control_audit_record_hash,
    decode_cursor,
    default_safe_kill_switch,
    encode_cursor,
    load_control_schema,
    validate_control_contract,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

# Fields that must never appear as a key anywhere in any E4 projection or
# response the frontend consumes.
FORBIDDEN_FIELDS = {
    "access_token",
    "account_id",
    "arn",
    "credential",
    "display_name",
    "email",
    "fleet_id",
    "id_token",
    "password",
    "provider_payload",
    "provider_response",
    "refresh_token",
    "secret",
    "session_token",
    "token",
}

VALID = {
    KILL_SWITCH_SCHEMA_NAME: "operations-kill-switch.valid.json",
    CAPABILITY_DISCOVERY_SCHEMA_NAME: "operations-capability-discovery.valid.json",
    LIST_REQUEST_SCHEMA_NAME: "operations-list-request.valid.json",
    LIST_RESPONSE_SCHEMA_NAME: "operations-list-response.valid.json",
    DETAIL_PROJECTION_SCHEMA_NAME: "operations-detail-projection.valid.json",
    CONTROL_REQUEST_SCHEMA_NAME: "operations-control-request.valid.json",
    CONTROL_RESPONSE_SCHEMA_NAME: "operations-control-response.valid.json",
    CONTROL_AUDIT_RECORD_SCHEMA_NAME: "operations-control-audit-record.valid.json",
}

_CURSOR_KEY = b"unit-test-cursor-signing-key"


def _fixture(schema_name: str) -> dict[str, Any]:
    return load_json(FIXTURES / VALID[schema_name])


def _all_keys(document: Any) -> set[str]:
    if isinstance(document, dict):
        return set(document).union(*(_all_keys(v) for v in document.values()), set())
    if isinstance(document, list):
        return set().union(*(_all_keys(v) for v in document), set())
    return set()


# -- Additive boundary -----------------------------------------------------


def test_control_schemas_never_join_a_published_registry():
    assert not (CONTROL_SCHEMA_NAMES & SCHEMA_NAMES)
    assert not (CONTROL_SCHEMA_NAMES & CAPACITY_SCHEMA_NAMES)
    assert not (CONTROL_SCHEMA_NAMES & EXECUTION_SCHEMA_NAMES)
    assert len(CONTROL_SCHEMA_NAMES) == 8


def test_control_schemas_are_self_contained_and_meta_valid():
    for schema_name in CONTROL_SCHEMA_NAMES:
        schema = load_control_schema(schema_name)
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("urn:game-agent:operations:contracts:v1:")


# -- All fixtures validate -------------------------------------------------


@pytest.mark.parametrize("schema_name", sorted(CONTROL_SCHEMA_NAMES))
def test_valid_fixture_passes(schema_name):
    validate_control_contract(schema_name, _fixture(schema_name))


def test_default_safe_kill_switch_disables_everything():
    document = default_safe_kill_switch(
        config_version=1,
        issued_at="2026-01-15T00:00:00Z",
        not_after="2026-01-15T00:05:00Z",
    )
    validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)
    assert document["operations_enabled"] is False
    switches = document["capabilities"][CAPABILITY_ID]
    assert switches == {"prepare": False, "dispatch": False, "execute": False}


# -- Public-safe exclusions ------------------------------------------------


@pytest.mark.parametrize(
    "schema_name",
    [
        CAPABILITY_DISCOVERY_SCHEMA_NAME,
        LIST_RESPONSE_SCHEMA_NAME,
        DETAIL_PROJECTION_SCHEMA_NAME,
        CONTROL_RESPONSE_SCHEMA_NAME,
        CONTROL_AUDIT_RECORD_SCHEMA_NAME,
    ],
)
def test_projection_fixture_carries_no_forbidden_field(schema_name):
    assert not (_all_keys(_fixture(schema_name)) & FORBIDDEN_FIELDS)


@pytest.mark.parametrize(
    "schema_name",
    [DETAIL_PROJECTION_SCHEMA_NAME, LIST_RESPONSE_SCHEMA_NAME, CONTROL_REQUEST_SCHEMA_NAME],
)
def test_injecting_a_forbidden_field_is_rejected(schema_name):
    document = _fixture(schema_name)
    document["email"] = "attacker@example.com"
    with pytest.raises(ControlContractError):
        validate_control_contract(schema_name, document)


# -- Kill-switch invariants ------------------------------------------------


def test_kill_switch_rejects_unknown_capability():
    document = _fixture(KILL_SWITCH_SCHEMA_NAME)
    document["capabilities"]["eks.node-scaling"] = {"prepare": False, "dispatch": False, "execute": False}
    with pytest.raises(ControlContractError):
        validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)


def test_kill_switch_rejects_unknown_top_level_field():
    document = _fixture(KILL_SWITCH_SCHEMA_NAME)
    document["environment"] = "production"
    with pytest.raises(ControlContractError):
        validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)


def test_kill_switch_requires_not_after_after_issued_at():
    document = _fixture(KILL_SWITCH_SCHEMA_NAME)
    document["not_after"] = document["issued_at"]
    with pytest.raises(ControlContractError):
        validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)


def test_kill_switch_enforces_phase_ordering():
    document = _fixture(KILL_SWITCH_SCHEMA_NAME)
    document["capabilities"][CAPABILITY_ID] = {"prepare": False, "dispatch": True, "execute": False}
    with pytest.raises(ControlContractError):
        validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)


def test_kill_switch_master_switch_off_forbids_enabled_phase():
    document = _fixture(KILL_SWITCH_SCHEMA_NAME)
    document["operations_enabled"] = False
    with pytest.raises(ControlContractError):
        validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)


# -- Capability discovery invariants ---------------------------------------


def test_discovery_enabled_requires_provisioned():
    document = _fixture(CAPABILITY_DISCOVERY_SCHEMA_NAME)
    document["capabilities"][0]["provisioned"] = False
    with pytest.raises(ControlContractError):
        validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, document)


def test_discovery_provisioned_requires_available():
    document = _fixture(CAPABILITY_DISCOVERY_SCHEMA_NAME)
    document["capabilities"][0]["available"] = False
    with pytest.raises(ControlContractError):
        validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, document)


def test_discovery_disabled_deployment_forbids_enabled_capability():
    document = _fixture(CAPABILITY_DISCOVERY_SCHEMA_NAME)
    document["deployment_mode"] = "disabled"
    document["operations_enabled"] = False
    with pytest.raises(ControlContractError):
        validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, document)


def test_discovery_enabled_requires_all_gates_satisfied():
    document = _fixture(CAPABILITY_DISCOVERY_SCHEMA_NAME)
    document["capabilities"][0]["gates"][-1]["satisfied"] = False
    with pytest.raises(ControlContractError):
        validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, document)


def test_discovery_carries_static_and_dynamic_gates():
    document = _fixture(CAPABILITY_DISCOVERY_SCHEMA_NAME)
    kinds = {gate["kind"] for gate in document["capabilities"][0]["gates"]}
    assert kinds == {"static", "dynamic"}


# -- List bounds -----------------------------------------------------------


def test_list_request_rejects_page_size_over_max():
    document = _fixture(LIST_REQUEST_SCHEMA_NAME)
    document["page_size"] = MAX_PAGE_SIZE + 1
    with pytest.raises(ControlContractError):
        validate_control_contract(LIST_REQUEST_SCHEMA_NAME, document)


def test_list_response_rejects_more_operations_than_page_size():
    document = _fixture(LIST_RESPONSE_SCHEMA_NAME)
    document["page_size"] = 1
    with pytest.raises(ControlContractError):
        validate_control_contract(LIST_RESPONSE_SCHEMA_NAME, document)


def test_list_response_rejects_over_fifty_operations():
    document = _fixture(LIST_RESPONSE_SCHEMA_NAME)
    template = document["operations"][0]
    document["operations"] = [
        {**deepcopy(template), "operation_id": f"op_{index:026d}"} for index in range(MAX_PAGE_SIZE + 1)
    ]
    document["page_size"] = MAX_PAGE_SIZE
    with pytest.raises(ControlContractError):
        validate_control_contract(LIST_RESPONSE_SCHEMA_NAME, document)


# -- Detail projection visibility ------------------------------------------


def test_detail_projection_verification_visibility_is_consistent():
    document = _fixture(DETAIL_PROJECTION_SCHEMA_NAME)
    document["verification"]["applicable"] = False
    with pytest.raises(ControlContractError):
        validate_control_contract(DETAIL_PROJECTION_SCHEMA_NAME, document)


def test_detail_projection_exposes_lifecycle_and_rollback():
    document = _fixture(DETAIL_PROJECTION_SCHEMA_NAME)
    phases = {phase["phase"] for phase in document["phases"]}
    assert {"prepare", "dispatch", "execute", "verify", "rollback"} <= phases
    assert set(document["rollback"]) == {"applicable", "outcome"}


# -- Control request/response ----------------------------------------------


def test_control_request_carries_only_booleans_and_expected_version():
    document = _fixture(CONTROL_REQUEST_SCHEMA_NAME)
    assert set(document) == {"contract_version", "expected_config_version", "desired"}
    # Injecting any identity/policy field is rejected by the closed object.
    for field in ("principal", "policy", "actor", "credential"):
        mutated = deepcopy(document)
        mutated[field] = {"anything": True}
        with pytest.raises(ControlContractError):
            validate_control_contract(CONTROL_REQUEST_SCHEMA_NAME, mutated)


def test_control_response_applied_requires_matching_effective_version():
    document = _fixture(CONTROL_RESPONSE_SCHEMA_NAME)
    document["effective"]["config_version"] = document["config_version"] + 1
    with pytest.raises(ControlContractError):
        validate_control_contract(CONTROL_RESPONSE_SCHEMA_NAME, document)


def test_control_response_conflict_must_not_carry_effective():
    document = _fixture(CONTROL_RESPONSE_SCHEMA_NAME)
    document["outcome"] = "version_conflict"
    document["reason_code"] = "VERSION_CONFLICT"
    with pytest.raises(ControlContractError):
        validate_control_contract(CONTROL_RESPONSE_SCHEMA_NAME, document)


# -- Immutable audit record ------------------------------------------------


def test_audit_hash_binds_every_field_and_excludes_itself():
    document = _fixture(CONTROL_AUDIT_RECORD_SCHEMA_NAME)
    assert document["record_hash"] == control_audit_record_hash(document)
    # Excludes only itself: recomputing after clearing the hash is stable.
    cleared = {**document, "record_hash": "sha256:" + "0" * 64}
    assert control_audit_record_hash(cleared) == document["record_hash"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": "denied"},
        {"resulting_config_version": 9},
        {"previous_config_version": 6},
        {"actor": {"subject_id": "someone-else", "client_id": "operations-console"}},
    ],
)
def test_audit_record_mutation_breaks_the_hash(mutation):
    document = _fixture(CONTROL_AUDIT_RECORD_SCHEMA_NAME)
    document.update(deepcopy(mutation))
    with pytest.raises(ControlContractError):
        validate_control_contract(CONTROL_AUDIT_RECORD_SCHEMA_NAME, document)


def test_audit_applied_must_advance_version():
    document = _fixture(CONTROL_AUDIT_RECORD_SCHEMA_NAME)
    document["resulting_config_version"] = document["previous_config_version"]
    document["record_hash"] = control_audit_record_hash(document)
    with pytest.raises(ControlContractError):
        validate_control_contract(CONTROL_AUDIT_RECORD_SCHEMA_NAME, document)


# -- Opaque cursor codec ---------------------------------------------------


def test_cursor_round_trips_and_is_deterministic():
    position = {"last_operation_id": "op_00000000000000000000000002", "last_created_at": "2026-01-15T00:00:05Z"}
    token = encode_cursor(position, key=_CURSOR_KEY)
    assert decode_cursor(token, key=_CURSOR_KEY) == position
    reordered = {"last_created_at": position["last_created_at"], "last_operation_id": position["last_operation_id"]}
    assert encode_cursor(reordered, key=_CURSOR_KEY) == token


@pytest.mark.parametrize("break_token", ["flip_payload", "flip_signature", "truncate", "empty_segment"])
def test_cursor_rejects_tamper(break_token):
    position = {"last_operation_id": "op_00000000000000000000000002"}
    token = encode_cursor(position, key=_CURSOR_KEY)
    if break_token == "flip_payload":
        bad = ("A" if token[0] != "A" else "B") + token[1:]
    elif break_token == "flip_signature":
        payload, _, signature = token.partition(".")
        bad = payload + "." + (("A" if signature[0] != "A" else "B") + signature[1:])
    elif break_token == "truncate":
        bad = token.split(".")[0]
    else:
        bad = "." + token.split(".")[1]
    with pytest.raises(CursorError):
        decode_cursor(bad, key=_CURSOR_KEY)


def test_cursor_rejects_wrong_key():
    token = encode_cursor({"last_operation_id": "op_00000000000000000000000002"}, key=_CURSOR_KEY)
    with pytest.raises(CursorError):
        decode_cursor(token, key=b"a-different-key")


def test_cursor_requires_non_empty_key():
    with pytest.raises(CursorError):
        encode_cursor({"a": 1}, key=b"")
    with pytest.raises(CursorError):
        decode_cursor("abc.def", key=b"")


# -- Frozen routes ---------------------------------------------------------


def test_frozen_route_keys_are_stable():
    assert ROUTE_KEYS == {
        "capabilities": "GET /operations/capabilities",
        "operations_list": "GET /operations",
        "operation_detail": "GET /operations/{operationId}",
        "control": "POST /operations/control",
        "kill_switch": "GET /operations/control/kill-switch",
    }
    for route_key in ROUTE_KEYS.values():
        method, _, path = route_key.partition(" ")
        assert method in {"GET", "POST"}
        assert path.startswith("/operations")


# -- Agent reference examples ----------------------------------------------

_SCHEMA_ID_TO_NAME = {f"urn:game-agent:operations:contracts:v1:{name}": name for name in CONTROL_SCHEMA_NAMES}


def test_agent_examples_all_validate_and_match_routes():
    examples = load_json(FIXTURES / "control-plane-examples.json")
    assert examples["routes"] == ROUTE_KEYS
    assert not (_all_keys(examples["examples"]) & FORBIDDEN_FIELDS)
    for key, entry in examples["examples"].items():
        schema_name = _SCHEMA_ID_TO_NAME[entry["schema_id"]]
        validate_control_contract(schema_name, entry["value"])
