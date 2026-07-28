#!/usr/bin/env python3
"""Unit tests for request-scoped context isolation (``utils.request_context``).

These verify the contract the Source Control Connector depends on for deriving
the authenticated ``Requesting_User`` identity from the request rather than from
model/tool input:

* ``get_request_context`` returns the default (``{}``) when nothing is set.
* ``set_request_context`` followed by ``get_request_context`` returns the value.
* ``reset_request_context`` restores the previously-observed value.
* Two sequential "invocations" do not leak identity across requests — the second
  invocation observes the default, never the first invocation's value.

Requirements: 7.1, 6.3
"""

# Standard library
from contextvars import copy_context

# Third-party packages
import pytest

# Local modules
from utils.request_context import (
    get_request_context,
    reset_request_context,
    set_request_context,
)

pytestmark = pytest.mark.unit


def test_get_returns_default_empty_dict_when_unset():
    """With no context set for the invocation, reads return the empty default."""
    # Run in a fresh copied context so no prior set can influence this read.
    assert copy_context().run(get_request_context) == {}


def test_set_then_get_returns_the_set_value():
    """Within a request, set makes the value observable via get."""

    def scenario():
        ctx = {"user_id": "alice", "groups": ["scm-writers"], "session_id": "s-1"}
        token = set_request_context(ctx)
        try:
            assert get_request_context() == ctx
        finally:
            reset_request_context(token)

    copy_context().run(scenario)


def test_reset_restores_previous_value():
    """Reset restores exactly the value observed before the matching set."""

    def scenario():
        # Baseline within this invocation is the default.
        assert get_request_context() == {}

        outer = {"user_id": "outer", "groups": ["a"], "session_id": "s-outer"}
        outer_token = set_request_context(outer)
        assert get_request_context() == outer

        # A nested set (e.g. re-entrancy) followed by reset restores the outer value.
        inner = {"user_id": "inner", "groups": ["b"], "session_id": "s-inner"}
        inner_token = set_request_context(inner)
        assert get_request_context() == inner
        reset_request_context(inner_token)
        assert get_request_context() == outer

        # Resetting the outer token restores the default baseline.
        reset_request_context(outer_token)
        assert get_request_context() == {}

    copy_context().run(scenario)


def test_non_dict_context_is_coerced_to_empty_dict():
    """A non-dict context is coerced so downstream reads stay safe."""

    def scenario():
        token = set_request_context("not-a-dict")  # type: ignore[arg-type]
        try:
            assert get_request_context() == {}
        finally:
            reset_request_context(token)

    copy_context().run(scenario)


def test_no_leakage_across_sequential_invocations():
    """A second invocation never observes the first invocation's identity.

    Each invocation runs in its own copied context and follows the
    set-in-try / reset-in-finally pattern used by ``invoke_agent``. The second
    invocation must see the default (``{}``) at entry, proving identity does not
    leak across requests.
    """
    first_identity = {"user_id": "first-user", "groups": ["scm-writers"], "session_id": "s-1"}
    observed = {}

    def first_invocation():
        # Entry to a request starts from the default.
        observed["first_entry"] = get_request_context()
        token = set_request_context(first_identity)
        try:
            observed["first_during"] = get_request_context()
        finally:
            reset_request_context(token)
        observed["first_exit"] = get_request_context()

    def second_invocation():
        # A fresh, independent request must not inherit the first's identity.
        observed["second_entry"] = get_request_context()

    copy_context().run(first_invocation)
    copy_context().run(second_invocation)

    assert observed["first_entry"] == {}
    assert observed["first_during"] == first_identity
    assert observed["first_exit"] == {}
    # The load-bearing assertion: no cross-invocation leakage.
    assert observed["second_entry"] == {}
    assert observed["second_entry"] != first_identity


def test_context_isolated_between_concurrent_copied_contexts():
    """Distinct contexts hold distinct values simultaneously (per-invocation isolation)."""
    ctx_a = {"user_id": "a"}
    ctx_b = {"user_id": "b"}
    results = {}

    def run_with(identity, key):
        token = set_request_context(identity)
        try:
            results[key] = get_request_context()
        finally:
            reset_request_context(token)

    context_a = copy_context()
    context_b = copy_context()
    context_a.run(run_with, ctx_a, "a")
    context_b.run(run_with, ctx_b, "b")

    assert results["a"] == ctx_a
    assert results["b"] == ctx_b
    # The base context is untouched by either isolated run.
    assert get_request_context() == {}
