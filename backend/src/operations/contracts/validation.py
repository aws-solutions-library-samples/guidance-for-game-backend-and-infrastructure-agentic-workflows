"""JSON Schema and semantic validation for immutable operations contracts."""

from __future__ import annotations

# Standard library
from functools import lru_cache
from importlib.resources import files
from typing import Any

# Third-party packages
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

# Local modules
from operations.contracts.canonical import canonical_sha256, load_json
from operations.contracts.source_control import (
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


class ContractValidationError(ValueError):
    """A document failed its versioned schema or semantic contract."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        self.schema_name = schema_name
        self.errors = tuple(errors)
        super().__init__(f"{schema_name} contract validation failed: {'; '.join(errors)}")


def _schema_directory():
    return files("operations.contracts").joinpath("schemas", "v1")


@lru_cache(maxsize=None)
def load_schema(schema_name: str) -> dict[str, Any]:
    """Load one immutable v1 schema by its public short name."""
    if schema_name not in SCHEMA_NAMES:
        raise ValueError(f"unknown operations contract schema: {schema_name}")
    schema_path = _schema_directory().joinpath(f"{schema_name}.schema.json")
    document = load_json(schema_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"contract schema must be a JSON object: {schema_name}")
    return document


@lru_cache(maxsize=1)
def _schema_registry() -> Registry:
    resources = []
    for schema_name in SCHEMA_NAMES:
        schema = load_schema(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


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
        return errors

    if schema_name == "source-control-prepared-operation":
        return source_control_semantic_errors(document)

    if schema_name == "prepare-operation-request":
        return source_control_request_semantic_errors(document)

    if schema_name == "authorization-decision":
        authority_inputs = document["authority_inputs"].values()
        expected = min(authority_inputs, key=_AUTHORITY_ORDER.__getitem__)
        if document["effective_authority"] != expected:
            return ["effective_authority is not the lowest authority input"]

    if schema_name == "operation-state-change":
        try:
            validate_state_transition(document["previous_state"], document["new_state"])
        except ValueError as exc:
            return [str(exc)]

    return []


def validate_contract(schema_name: str, document: object) -> None:
    """Validate a document against its exact schema and semantic invariants."""
    schema = load_schema(schema_name)
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


def validate_playbook_binding(prepared_operation: dict[str, Any], playbook: dict[str, Any]) -> None:
    """Verify that a prepared operation names and hashes one exact playbook."""
    validate_contract("prepared-operation", prepared_operation)
    validate_contract("playbook", playbook)

    reference = prepared_operation["playbook"]
    errors = []
    if reference["playbook_id"] != playbook["playbook_id"]:
        errors.append("playbook_id does not match the prepared operation")
    if reference["playbook_version"] != playbook["playbook_version"]:
        errors.append("playbook_version does not match the prepared operation")
    if reference["playbook_hash"] != canonical_sha256(playbook):
        errors.append("playbook_hash does not match the canonical playbook")
    if errors:
        raise ContractValidationError("playbook-binding", errors)


def validate_approval_binding(approval: dict[str, Any], prepared_operation: dict[str, Any]) -> None:
    """Verify that approval grants authority only to one exact stored operation."""
    validate_contract("approval-record", approval)
    validate_contract("prepared-operation", prepared_operation)

    errors = []
    if approval["operation_id"] != prepared_operation["operation_id"]:
        errors.append("approval operation_id does not match the prepared operation")
    if approval["prepared_operation_hash"] != canonical_sha256(prepared_operation):
        errors.append("approval hash does not match the canonical prepared operation")
    if errors:
        raise ContractValidationError("approval-binding", errors)
