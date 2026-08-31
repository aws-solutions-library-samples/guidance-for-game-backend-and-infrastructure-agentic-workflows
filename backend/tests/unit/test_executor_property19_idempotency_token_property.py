#!/usr/bin/env python3
"""Property-based test for idempotency-token stability.

Feature: source-control-connector-executor, Property 19 (design → Correctness Properties).
Repeated derivations for the *same* operation produce the same Idempotency_Token (it is stable
across retries), while two operations with identical content share a duplicate-content key yet
retain distinct operation identity and distinct idempotency tokens — so retry idempotency and
duplicate-content detection never conflate (Req 6.7, 6.8).

The two derivations are exercised through :func:`connector.executor.opid.idempotency_token`
and :func:`connector.executor.opid.duplicate_content_key`, both built on the baseline
order-independent, content-addressed serialization reused from
:func:`connector.service._idempotency_key`.

Validates: Requirements 6.7, 6.8
"""

# Third-party packages
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# Local modules
from connector.executor import opid
from connector.models import ProposedFile

pytestmark = pytest.mark.unit


@st.composite
def _files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    bodies = draw(st.lists(st.text(min_size=0, max_size=30), min_size=len(names), max_size=len(names)))
    return tuple(
        ProposedFile(path=f"infra/{name}.yaml", content=body, iac_format="cloudformation")
        for name, body in zip(names, bodies)
    )


_IDS = st.from_regex(r"[a-z0-9]{1,24}", fullmatch=True)


# Feature: source-control-connector-executor, Property 19: The idempotency token is stable across retries and independent of duplicate-content detection
@settings(max_examples=100)
@given(
    files=_files(),
    repo=st.from_regex(r"[a-z]{2,8}/[a-z]{2,8}", fullmatch=True),
    branch=st.from_regex(r"[a-z][a-z0-9-]{1,10}", fullmatch=True),
    base_revision=st.from_regex(r"[0-9a-f]{40}", fullmatch=True),
    op_id_a=_IDS,
    op_id_b=_IDS,
)
def test_property19_token_stable_and_independent_of_duplicate_content(
    files: tuple[ProposedFile, ...],
    repo: str,
    branch: str,
    base_revision: str,
    op_id_a: str,
    op_id_b: str,
) -> None:
    """The token is stable across retries of one operation, yet two operations with identical
    content share a duplicate-content key while keeping distinct identity/tokens (Req 6.7,
    6.8)."""
    kwargs = dict(repo=repo, target_branch=branch, base_revision=base_revision, files=files)

    # Stability across retries: the same operation re-derives the same token every time.
    token_a1 = opid.idempotency_token(operation_id=op_id_a, **kwargs)
    token_a2 = opid.idempotency_token(operation_id=op_id_a, **kwargs)
    assert token_a1 == token_a2

    # Duplicate-content detection is content-addressed and independent of operation identity:
    # two operations with identical content/target/base share the same duplicate-content key.
    dup_key = opid.duplicate_content_key(**kwargs)
    assert dup_key == opid.duplicate_content_key(**kwargs)

    # Two operations with byte-identical content but different ids: same duplicate-content key,
    # distinct identity, and distinct idempotency tokens (the two never conflate).
    assume(op_id_a != op_id_b)
    token_b = opid.idempotency_token(operation_id=op_id_b, **kwargs)
    assert token_a1 != token_b
    assert opid.duplicate_content_key(**kwargs) == dup_key
