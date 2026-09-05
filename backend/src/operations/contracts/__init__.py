"""Versioned contracts shared by every operations adapter and executor."""

# Local modules
from operations.contracts.canonical import CanonicalizationError, canonical_sha256, canonicalize, load_json
from operations.contracts.source_control import source_control_branch_name, source_control_content_hash
from operations.contracts.validation import (
    ContractValidationError,
    load_schema,
    validate_approval_binding,
    validate_authorization_binding,
    validate_contract,
    validate_playbook_binding,
    validate_prepared_operation,
)
from operations.contracts.versions import (
    CONTRACT_VERSION,
    OPERATION_STATES,
    SCHEMA_NAMES,
    is_supported_contract_version,
    validate_state_transition,
)

__all__ = [
    "CONTRACT_VERSION",
    "OPERATION_STATES",
    "SCHEMA_NAMES",
    "CanonicalizationError",
    "ContractValidationError",
    "canonical_sha256",
    "canonicalize",
    "is_supported_contract_version",
    "load_json",
    "load_schema",
    "source_control_branch_name",
    "source_control_content_hash",
    "validate_approval_binding",
    "validate_authorization_binding",
    "validate_contract",
    "validate_playbook_binding",
    "validate_prepared_operation",
    "validate_state_transition",
]
