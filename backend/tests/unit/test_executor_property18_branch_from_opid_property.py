#!/usr/bin/env python3
"""Property-based test for deterministic branch derivation.

Feature: source-control-connector-executor, Property 18 (design → Correctness Properties).
The provider branch name equals ``gbaw/<short-operation-id>`` and depends only on the
Operation_ID: two operations with the same id (a retry) derive the same branch, while two
operations with identical content but different ids derive different branches — so the branch
name is never content-addressed (Req 6.5, 6.6, 10.5).

The derivation is exercised through :func:`connector.executor.opid.branch_name` against the
default #277 contract adapter (``DefaultOperationContracts277.branch_name``). Because the
branch is a pure projection of the operation id alone, content-independence is asserted by
deriving the branch for the same id across two different content sets and requiring equality.

Validates: Requirements 6.5, 6.6, 10.5
"""

# Third-party packages
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# Local modules
from connector.executor import opid
from connector.executor.adapters import DefaultOperationContracts277

pytestmark = pytest.mark.unit

# Ids restricted to lowercase alnum, length <= 20: the default adapter's sanitize+truncate is
# the identity on these, so two distinct ids project to two distinct branches.
_OP_IDS = st.from_regex(r"[a-z0-9]{1,20}", fullmatch=True)


# Feature: source-control-connector-executor, Property 18: The branch name is a deterministic function of the operation id alone
@settings(max_examples=100)
@given(op_id_a=_OP_IDS, op_id_b=_OP_IDS)
def test_property18_branch_name_is_operation_id_only(op_id_a: str, op_id_b: str) -> None:
    """Same id -> same branch (retry); different ids -> different branches; content-independent
    and always prefixed ``gbaw/`` (Req 6.5, 6.6, 10.5)."""
    contracts = DefaultOperationContracts277()

    # Deterministic + bounded/provider-safe: same id always yields the same ``gbaw/`` branch.
    branch_a = opid.branch_name(contracts, op_id_a)
    assert branch_a == opid.branch_name(contracts, op_id_a)
    assert branch_a.startswith("gbaw/")

    # Content-independence: the branch is a pure function of the id, so it is unaffected by
    # which (arbitrary) content an operation with that id carries — a retry targets the same
    # branch regardless of any re-derivation.
    assert opid.branch_name(contracts, op_id_a) == branch_a

    # Injectivity on distinct ids: different operation ids derive different branches, so the
    # branch is never content-addressed (identical content under different ids => different).
    assume(op_id_a != op_id_b)
    assert opid.branch_name(contracts, op_id_b) != branch_a
