"""JSON Schema and semantic validation for immutable operations contracts."""

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
from operations.contracts.source_control import (
    ContentResolver,
    source_control_playbook_binding_errors,
    source_control_request_semantic_errors,
    source_control_semantic_errors,
)
from operations.contracts.versions import SCHEMA_NAMES, validate_state_transition

_AUTHORITY_ORDER = {
    "disabled": 0,
    "observe": 1,
    "advise": 2,
    "remediate": 3,
    "operate": 4,
}
_CURRENT_MINIMUM_AUTHORITY = "remediate"
_PROFILE_SCHEMAS = {
    "source-control.change-proposal/1.0": "source-control-prepared-operation",
}


class ContractValidationError(ValueError):
    """A document failed its versioned schema or semantic contract."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        self.schema_name = schema_name
        self.errors = tuple(errors)
        super().__init__(f"{schema_name} contract validation failed: {'; '.join(errors)}")


def _schema_directory():
    return files("operations.contracts").joinpath("schemas", "v1")


@lru_cache(maxsize=None)
def _load_schema_cached(schema_name: str) -> dict[str, Any]:
    if schema_name not in SCHEMA_NAMES:
        raise ValueError(f"unknown operations contract schema: {schema_name}")
    schema_path = _schema_directory().joinpath(f"{schema_name}.schema.json")
    document = load_json(schema_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"contract schema must be a JSON object: {schema_name}")
    return document


def load_schema(schema_name: str) -> dict[str, Any]:
    """Load a defensive copy of one immutable v1 schema."""
    return deepcopy(_load_schema_cached(schema_name))


@lru_cache(maxsize=1)
def _schema_registry() -> Registry:
    resources = []
    for schema_name in SCHEMA_NAMES:
        schema = _load_schema_cached(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


def _authorization_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    authority_inputs = document["authority_inputs"]
    expected = min(authority_inputs.values(), key=_AUTHORITY_ORDER.__getitem__)
    effective = document["effective_authority"]
    decision = document["decision"]
    reasons = set(document["reason_codes"])

    if effective != expected:
        errors.append("effective_authority is not the lowest authority input")

    if decision == "authorized" and reasons != {"AUTHORIZED"}:
        errors.append("authorized decision must have only the AUTHORIZED reason code")
    elif decision == "approval_required" and reasons != {"APPROVAL_REQUIRED"}:
        errors.append("approval_required decision must have only the APPROVAL_REQUIRED reason code")
    elif decision == "denied" and reasons & {"AUTHORIZED", "APPROVAL_REQUIRED"}:
        errors.append("denied decision cannot have an authorization reason code")

    if decision != "denied" and _AUTHORITY_ORDER[effective] < _AUTHORITY_ORDER[_CURRENT_MINIMUM_AUTHORITY]:
        errors.append(f"{decision} decision requires at least {_CURRENT_MINIMUM_AUTHORITY} authority")

    if authority_inputs["deployment_mode"] == "disabled":
        if decision != "denied":
            errors.append("disabled deployment mode requires a denied decision")
        if "DEPLOYMENT_DISABLED" not in reasons:
            errors.append("disabled deployment mode requires the DEPLOYMENT_DISABLED reason code")

    return errors


def _semantic_errors(schema_name: str, document: dict[str, Any]) -> list[str]:
    if schema_name == "playbook":
        errors = []
        expected_ids = {
            load_schema(name)["$id"]: canonical_sha256(load_schema(name))
            for name in SCHEMA_NAMES
            if name not in {"playbook"}
        }
        actual = {binding["schema_id"]: binding["schema_hash"] for binding in document["schemas"]}
        if len(actual) != len(document["schemas"]):
            errors.append("schemas contains a duplicate schema_id")
        if actual != expected_ids:
            errors.append("schemas does not bind the complete immutable v1 schema set")
        minimum = document["authority"]["minimum_mode"]
        maximum = document["authority"]["maximum_mode"]
        if _AUTHORITY_ORDER[minimum] > _AUTHORITY_ORDER[maximum]:
            errors.append("authority minimum_mode cannot exceed maximum_mode")
        return errors

    if schema_name == "source-control-prepared-operation":
        return source_control_semantic_errors(document)

    if schema_name == "prepare-operation-request":
        return source_control_request_semantic_errors(document)

    if schema_name == "authorization-decision":
        return _authorization_errors(document)

    if schema_name == "operation-state-change":
        try:
            validate_state_transition(document["previous_state"], document["new_state"], document["reason_code"])
        except ValueError as exc:
            return [str(exc)]

    return []


def validate_contract(schema_name: str, document: object) -> None:
    """Validate a document against its exact schema and semantic invariants."""
    schema = load_schema(schema_name)
    try:
        canonicalize(document)
    except CanonicalizationError as exc:
        raise ContractValidationError(schema_name, [f"document is outside the canonical I-JSON domain: {exc}"]) from exc

    validator = Draft202012Validator(
        schema,
        registry=_schema_registry(),
        format_checker=FormatChecker(),
    )
    schema_errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    errors = [_format_path(error) for error in schema_errors]

    if not errors and isinstance(document, dict):
        errors.extend(_semantic_errors(schema_name, document))

    if errors:
        raise ContractValidationError(schema_name, errors)


def validate_prepared_operation(prepared_operation: dict[str, Any]) -> None:
    """Validate the core and exact named profile of a prepared operation."""
    validate_contract("prepared-operation", prepared_operation)
    profile = prepared_operation["profile"]
    profile_schema = _PROFILE_SCHEMAS.get(profile)
    if profile_schema is None:
        raise ContractValidationError(
            "prepared-operation-profile", [f"unsupported prepared operation profile: {profile}"]
        )
    validate_contract(profile_schema, prepared_operation)


def validate_playbook_binding(
    prepared_operation: dict[str, Any],
    playbook: dict[str, Any],
    *,
    content_resolver: ContentResolver | None = None,
) -> None:
    """Verify that a prepared operation satisfies and binds one exact playbook."""
    validate_prepared_operation(prepared_operation)
    validate_contract("playbook", playbook)

    reference = prepared_operation["playbook"]
    errors = []
    if reference["playbook_id"] != playbook["playbook_id"]:
        errors.append("playbook_id does not match the prepared operation")
    if reference["playbook_version"] != playbook["playbook_version"]:
        errors.append("playbook_version does not match the prepared operation")
    if reference["playbook_hash"] != canonical_sha256(playbook):
        errors.append("playbook_hash does not match the canonical playbook")
    if prepared_operation["profile"] != playbook["profile"]:
        errors.append("profile does not match the playbook")
    if prepared_operation["retry_policy"] != playbook["retry_policy"]:
        errors.append("retry_policy does not match the playbook")
    if prepared_operation["executor_binding"] != playbook["executor_binding"]:
        errors.append("executor_binding does not match the playbook")
    errors.extend(source_control_playbook_binding_errors(prepared_operation, playbook, content_resolver))
    if errors:
        raise ContractValidationError("playbook-binding", errors)


def validate_authorization_binding(
    authorization: dict[str, Any],
    prepared_operation: dict[str, Any],
    playbook: dict[str, Any],
    *,
    content_resolver: ContentResolver | None = None,
) -> None:
    """Verify one authorization decision against its operation and playbook."""
    validate_contract("authorization-decision", authorization)
    validate_playbook_binding(prepared_operation, playbook, content_resolver=content_resolver)

    errors = []
    if authorization["operation_id"] != prepared_operation["operation_id"]:
        errors.append("authorization operation_id does not match the prepared operation")
    if authorization["prepared_operation_hash"] != canonical_sha256(prepared_operation):
        errors.append("authorization hash does not match the canonical prepared operation")
    if authorization["principal"] != prepared_operation["requester"]:
        errors.append("authorization principal does not match the prepared operation requester")
    if authorization["policy_version"] != prepared_operation["policy"]["policy_version"]:
        errors.append("authorization policy_version does not match the prepared operation")

    decision = authorization["decision"]
    effective = authorization["effective_authority"]
    minimum = playbook["authority"]["minimum_mode"]
    maximum = playbook["authority"]["maximum_mode"]
    if decision != "denied" and _AUTHORITY_ORDER[effective] < _AUTHORITY_ORDER[minimum]:
        errors.append("authorization authority is below the playbook minimum")
    if decision != "denied" and _AUTHORITY_ORDER[effective] > _AUTHORITY_ORDER[maximum]:
        errors.append("authorization authority exceeds the playbook maximum")
    if decision == "authorized" and playbook["authority"]["approval_requirement"] == "required":
        errors.append("authorization cannot bypass the playbook approval requirement")

    if errors:
        raise ContractValidationError("authorization-binding", errors)


def validate_approval_binding(approval: dict[str, Any], prepared_operation: dict[str, Any]) -> None:
    """Verify that one granted approval authorizes one exact stored operation."""
    validate_contract("approval-record", approval)
    validate_prepared_operation(prepared_operation)

    errors = []
    if approval["decision"] != "granted":
        errors.append("approval decision is not granted")
    if approval["operation_id"] != prepared_operation["operation_id"]:
        errors.append("approval operation_id does not match the prepared operation")
    if approval["prepared_operation_hash"] != canonical_sha256(prepared_operation):
        errors.append("approval hash does not match the canonical prepared operation")
    if approval["policy_version"] != prepared_operation["policy"]["policy_version"]:
        errors.append("approval policy_version does not match the prepared operation")
    if errors:
        raise ContractValidationError("approval-binding", errors)
