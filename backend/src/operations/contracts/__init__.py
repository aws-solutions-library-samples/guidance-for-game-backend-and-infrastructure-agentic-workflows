"""Versioned contracts shared by every operations adapter and executor."""

# Local modules
from operations.contracts.canonical import CanonicalizationError, canonical_sha256, canonicalize, load_json
from operations.contracts.capacity import (
    CAPACITY_SCHEMA_NAMES,
    CapacityContractError,
    calculate_capacity_risk,
    capacity_bounds_violations,
    capacity_change,
    capacity_prepared_hash,
    load_capacity_schema,
    validate_capacity_contract,
    validate_prepared_operation_binding,
)
from operations.contracts.execution import (
    EXECUTION_SCHEMA_NAMES,
    ExecutionContractError,
    build_execution_intent,
    execution_intent_hash,
    load_execution_schema,
    logical_action_id,
    validate_execution_contract,
    validate_execution_intent_binding,
)
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
    "CAPACITY_SCHEMA_NAMES",
    "CONTRACT_VERSION",
    "EXECUTION_SCHEMA_NAMES",
    "OPERATION_STATES",
    "SCHEMA_NAMES",
    "CanonicalizationError",
    "CapacityContractError",
    "ContractValidationError",
    "ExecutionContractError",
    "build_execution_intent",
    "calculate_capacity_risk",
    "canonical_sha256",
    "canonicalize",
    "capacity_bounds_violations",
    "capacity_change",
    "capacity_prepared_hash",
    "execution_intent_hash",
    "is_supported_contract_version",
    "load_capacity_schema",
    "load_execution_schema",
    "load_json",
    "load_schema",
    "logical_action_id",
    "source_control_branch_name",
    "source_control_content_hash",
    "validate_approval_binding",
    "validate_authorization_binding",
    "validate_capacity_contract",
    "validate_contract",
    "validate_execution_contract",
    "validate_execution_intent_binding",
    "validate_playbook_binding",
    "validate_prepared_operation",
    "validate_prepared_operation_binding",
    "validate_state_transition",
]
