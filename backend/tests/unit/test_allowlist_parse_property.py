#!/usr/bin/env python3
"""Property-based tests for the repository-allowlist grammar (`connector/config.py`).

Covers Correctness Property 4 from the source-control-connector design: the
`GBAW_SCM_REPO_ALLOWLIST` grammar round-trips. The grammar is::

    allowlist := entry ( ";" entry )*
    entry     := repo "=" branch ( "," branch )*

For any mapping of repositories to non-empty branch sets, encoding it into that
grammar and parsing it back must yield an allowlist whose entries represent exactly
the original repository -> branches mapping. This is what guarantees the operator's
configured allowlist is faithfully reconstructed before it gates any write (Req 5.1).

Validates: Requirements 5.1
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from config import settings as app_settings
from connector.config import AllowlistEntry, SourceControlConfig, _parse_allowlist

pytestmark = pytest.mark.unit


# --- Hypothesis strategies -------------------------------------------------

# A repo/branch token that is safe under the grammar: it contains none of the
# grammar delimiters ("=", ";", ",") and no whitespace (the parser strips segments,
# so leading/trailing whitespace would not survive a round-trip). This still covers
# realistic identifiers like "org/iac-repo" and "release/1.0".
_tokens = st.from_regex(r"[A-Za-z0-9_./-]{1,20}", fullmatch=True)


@st.composite
def _repo_branch_mapping(draw) -> dict[str, list[str]]:
    """A mapping of unique repositories to a non-empty set of unique branches.

    Repositories are unique (a mapping has distinct keys) and every repository maps
    to at least one branch, matching the "non-empty branch sets" quantifier of the
    property. Branches within a repo are unique so the comparison is unambiguous.
    """
    repos = draw(st.lists(_tokens, min_size=1, max_size=5, unique=True))
    return {
        repo: draw(st.lists(_tokens, min_size=1, max_size=4, unique=True))
        for repo in repos
    }


def _serialize(mapping: dict[str, list[str]]) -> str:
    """Encode a repo -> branches mapping into the GBAW_SCM_REPO_ALLOWLIST grammar."""
    return ";".join(f"{repo}={','.join(branches)}" for repo, branches in mapping.items())


# --- Property 4 ------------------------------------------------------------


# Feature: source-control-connector, Property 4: Allowlist parse round-trip
@settings(max_examples=100)
@given(mapping=_repo_branch_mapping())
def test_property4_allowlist_parse_round_trip(mapping):
    """Encoding a mapping into the grammar and parsing it back reproduces it exactly.

    The parsed entries must represent precisely the original repository -> branches
    mapping: same repositories, each with the same ordered branches, no extra or
    missing entries, and no parse errors.
    """
    raw = _serialize(mapping)

    entries, errors = _parse_allowlist(raw)

    assert errors == []
    assert len(entries) == len(mapping)
    # Every entry is a well-formed AllowlistEntry.
    assert all(isinstance(e, AllowlistEntry) for e in entries)
    # The reconstructed mapping equals the original mapping exactly.
    reconstructed = {e.repo: list(e.target_branches) for e in entries}
    assert reconstructed == mapping


# Feature: source-control-connector, Property 4: Allowlist parse round-trip
@settings(max_examples=100)
@given(mapping=_repo_branch_mapping())
def test_property4_allowlist_round_trip_via_config_load(mapping):
    """The round-trip holds through the public `ConnectorConfig.load()` path too.

    Driving parsing via `load()` (with the enablement flag truthy so parsing runs)
    and reading `config.allowlist` proves the operator-facing grammar reconstructs
    the exact mapping regardless of any other config validation outcome. Settings are
    patched with context managers (not the function-scoped `monkeypatch` fixture) so
    each Hypothesis-generated input is exercised in isolation.
    """
    raw = _serialize(mapping)
    with (
        mock.patch.object(app_settings, "SCM_CONNECTOR_ENABLED", "true"),
        mock.patch.object(app_settings, "SCM_REPO_ALLOWLIST", raw),
    ):
        config = SourceControlConfig.load()

    reconstructed = {
        e.repo: list(e.target_branches) for e in config.domain.authorization_policy
    }
    assert reconstructed == mapping
