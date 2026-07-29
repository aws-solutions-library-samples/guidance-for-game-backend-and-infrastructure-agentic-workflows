#!/usr/bin/env python3
"""Example test: the provider abstraction defines *only* the propose/read operation set.

This is a structural guarantee (not a runtime check): ``SourceControlProvider`` exposes a
fixed set of read + propose operations and deliberately defines **no** merge, approve,
close, delete, or other mutation/finalization operation. Because the Connector can only
call operations the abstraction declares, the absence of a merge/approve/close operation
makes it structurally impossible for the Connector to merge, approve, or close a
Change_Proposal.

Validates: Requirements 2.5, 6.2
"""

# Standard library
import inspect

# Third-party packages
import pytest

# Local modules
from connector.provider import SourceControlProvider

pytestmark = pytest.mark.unit


# The fixed set of *abstract* read/propose operations every adapter must implement (Req 9.2).
EXPECTED_OPERATIONS = {
    "get_file",
    "get_files",
    "branch_exists",
    "latest_commit_sha",
    "create_branch",
    "commit_files",
    "open_change_proposal",
}

# Optional, provider-neutral operations that carry a safe default implementation on the ABC
# (so they are NOT abstract) and are all read-only. Added by the hardening spec:
# ``find_open_change_proposal`` powers reconcile-before-retry (Req 12.4). Read-only, so it
# introduces no merge/approve/close/mutation capability.
OPTIONAL_OPERATIONS = {
    "find_open_change_proposal",
}

# The complete set of public operations the abstraction is permitted to expose.
ALL_PUBLIC_OPERATIONS = EXPECTED_OPERATIONS | OPTIONAL_OPERATIONS

# Substrings that would indicate a forbidden mutation/finalization operation on the
# proposal (or the repository) sneaking onto the abstraction (Req 2.5, 6.2).
FORBIDDEN_OPERATION_SUBSTRINGS = (
    "merge",
    "approve",
    "close",
    "delete",
    "rebase",
    "squash",
    "revert",
    "force",
    "push",  # a direct push would bypass the propose/review path
)


def _public_operations(cls) -> set:
    """Return the public (non-dunder, non-underscore) method names declared on ``cls``."""
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_abstract_method_set_is_exactly_the_propose_read_set():
    """The ABC's abstract methods are exactly the fixed read/propose operation set."""
    abstract_methods = set(getattr(SourceControlProvider, "__abstractmethods__", frozenset()))
    assert abstract_methods == EXPECTED_OPERATIONS


def test_public_operations_are_exactly_the_propose_read_set():
    """No public operation exists on the ABC beyond the read/propose set plus optional
    read-only reconciliation helpers."""
    assert _public_operations(SourceControlProvider) == ALL_PUBLIC_OPERATIONS


def test_no_merge_approve_or_close_operation_is_defined():
    """No mutation/finalization operation (merge/approve/close/delete/...) is defined."""
    public_ops = _public_operations(SourceControlProvider)
    offending = {
        op
        for op in public_ops
        for bad in FORBIDDEN_OPERATION_SUBSTRINGS
        if bad in op.lower()
    }
    assert offending == set(), f"forbidden mutation operation(s) present: {sorted(offending)}"


@pytest.mark.parametrize("forbidden", ["merge", "approve", "close", "delete"])
def test_specific_forbidden_operation_is_absent(forbidden):
    """The abstraction exposes no merge/approve/close/delete attribute of any kind."""
    assert not hasattr(SourceControlProvider, forbidden)
