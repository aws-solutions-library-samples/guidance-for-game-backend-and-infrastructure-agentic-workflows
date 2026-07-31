#!/usr/bin/env python3
"""Property-based test for the v2 three-layer domain/config separation gate.

Covers Correctness Property V1 from the source-control-connector-v2 design: the previously
monolithic connector configuration is split into three cohesive contracts —
:class:`DomainConfig` (authorization policy + authorized groups), the neutral
:class:`ConnectorConfig` (provider + rate/timeout/retry/max-files + audit destination), and
:class:`AdapterConfig` (credential ARN + provider base URL) — composed by
:class:`SourceControlConfig`. ``SourceControlConfig.load()`` is the single fail-closed
decision point: it resolves ``enabled == True`` **if and only if** the enablement flag is
truthy AND all three contracts validate. Any invalid contract forces ``enabled=False``,
records the offending values on that contract's ``config_errors``, and (on the
truthy-but-invalid path) emits exactly one sanitized configuration-error audit entry — so no
IaC_Change_Specialist is registered and the platform keeps its baseline tool set (V1 amends
baseline P1, P2, P3).

Config is driven by patching the ``GBAW_SCM_*`` values on ``config.settings`` (the module
``connector/config.py`` reads from), using a ``patch.multiple`` context manager applied and
torn down per generated example — the same approach the sibling config property tests use,
rather than the function-scoped ``monkeypatch`` fixture. ``load()`` performs no secret
retrieval, so no provider/Secrets-Manager mock is required; the configuration-error audit
logger is patched to observe the single audit entry.

Validates: Requirements 1.1, 1.3, 1.4, 1.5, 1.6, 10.1, 10.2, 10.3, 10.4
"""

# Standard library
import contextlib
import dataclasses
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
import connector.config as config_module
from config import settings as app_settings
from connector.config import (
    AdapterConfig,
    ConnectorConfig,
    DomainConfig,
    SourceControlConfig,
)

pytestmark = pytest.mark.unit

# The full set of GBAW_SCM_* attributes SourceControlConfig.load() reads from settings.
_SCM_ATTRS = (
    "SCM_CONNECTOR_ENABLED",
    "SCM_PROVIDER",
    "SCM_PROVIDER_BASE_URL",
    "SCM_CREDENTIAL_SECRET_ID",
    "SCM_REPO_ALLOWLIST",
    "SCM_AUTHORIZED_GROUPS",
    "SCM_AUDIT_LOG_GROUP",
    "SCM_RATE_LIMIT_MAX",
    "SCM_RATE_LIMIT_WINDOW_SECONDS",
    "SCM_PROVIDER_TIMEOUT_SECONDS",
    "SCM_RETRY_MAX_ATTEMPTS",
    "SCM_MAX_FILES_PER_REQUEST",
)

_TRUTHY = {"true", "1", "yes"}

# The exact field set each split contract is expected to own (excluding config_errors is
# NOT done here — config_errors is intentionally per-contract and must be present on each).
_DOMAIN_FIELDS = {"authorization_policy", "authorized_groups", "config_errors"}
_CONNECTOR_FIELDS = {
    "provider",
    "rate_limit_max",
    "rate_limit_window_seconds",
    "provider_timeout_seconds",
    "retry_max_attempts",
    "max_files_per_request",
    "audit_log_group",
    "config_errors",
}
_ADAPTER_FIELDS = {"credential_secret_arn", "provider_base_url", "config_errors"}


@contextlib.contextmanager
def _scm_settings(values: dict):
    """Patch every GBAW_SCM_* attribute on the settings module for one example.

    A ``patch.multiple`` context manager (not the ``monkeypatch`` fixture) is used so the
    values are applied and restored for *each* Hypothesis-generated input.
    """
    with patch.multiple(app_settings, **{attr: values[attr] for attr in _SCM_ATTRS}):
        yield


def _valid_base() -> dict:
    """A fully valid, enable-able configuration (all three contracts validate).

    Each scenario overrides the enablement flag and, independently, at most one field per
    contract, so a disablement is unambiguously attributable to the mutated field(s).
    """
    return {
        "SCM_CONNECTOR_ENABLED": "true",
        "SCM_PROVIDER": "github",
        "SCM_PROVIDER_BASE_URL": None,
        "SCM_CREDENTIAL_SECRET_ID": "scm/github-token",
        "SCM_REPO_ALLOWLIST": "org/iac-repo=main",
        "SCM_AUTHORIZED_GROUPS": "iac-admins",
        "SCM_AUDIT_LOG_GROUP": "scm-audit-logs",
        "SCM_RATE_LIMIT_MAX": "5",
        "SCM_RATE_LIMIT_WINDOW_SECONDS": "3600",
        "SCM_PROVIDER_TIMEOUT_SECONDS": "30",
        "SCM_RETRY_MAX_ATTEMPTS": "3",
        "SCM_MAX_FILES_PER_REQUEST": "20",
    }


# --- Strategies -------------------------------------------------------------

# Flags that ARE truthy (case-insensitive, whitespace-trimmed) and flags that are NOT.
_truthy_flags = st.sampled_from(["true", "1", "yes", "TRUE", "Yes", " yes ", "  1 ", "YES", "yEs"])
_non_truthy_flags = st.sampled_from(
    [None, "", "  ", "false", "no", "0", "off", "disabled", "FALSE", "2", "true-ish"]
)

# Each invalidation is (settings_key, bad_value, expected_error_substring), scoped so it
# breaks exactly one field of the named contract.

# DomainConfig: authorization policy (allowlist) + authorized groups.
_domain_invalidations = st.one_of(
    st.tuples(
        st.just("SCM_REPO_ALLOWLIST"),
        st.sampled_from([None, "", "   ", "no-equals-here", "=main", "org/repo=", ";;"]),
        st.just("allowlist"),
    ),
    st.tuples(
        st.just("SCM_AUTHORIZED_GROUPS"),
        st.sampled_from([None, "", " , , "]),
        st.just("authorized_groups"),
    ),
)

# ConnectorConfig (neutral): provider + rate/timeout/retry/max-files + audit destination.
_connector_invalidations = st.one_of(
    st.tuples(
        st.just("SCM_PROVIDER"),
        st.sampled_from([None, "", "   ", "gitlab", "bitbucket"]),
        st.just("provider"),
    ),
    st.tuples(
        st.just("SCM_AUDIT_LOG_GROUP"),
        st.sampled_from([None, "", "   "]),
        st.just("audit_log_group"),
    ),
    st.tuples(
        st.just("SCM_RATE_LIMIT_MAX"),
        st.sampled_from(["0", "1001", "-1", "abc"]),
        st.just("rate_limit_max"),
    ),
    st.tuples(
        st.just("SCM_RATE_LIMIT_WINDOW_SECONDS"),
        st.sampled_from(["59", "86401", "0", "xyz"]),
        st.just("rate_limit_window_seconds"),
    ),
    st.tuples(
        st.just("SCM_PROVIDER_TIMEOUT_SECONDS"),
        st.sampled_from(["0", "301", "-5", "foo"]),
        st.just("provider_timeout_seconds"),
    ),
    st.tuples(
        st.just("SCM_RETRY_MAX_ATTEMPTS"),
        st.sampled_from(["0", "11", "-2", "bar"]),
        st.just("retry_max_attempts"),
    ),
    st.tuples(
        st.just("SCM_MAX_FILES_PER_REQUEST"),
        st.sampled_from(["0", "-3", "abc"]),
        st.just("max_files_per_request"),
    ),
)

# AdapterConfig: credential secret ARN + provider base URL.
_adapter_invalidations = st.one_of(
    st.tuples(
        st.just("SCM_CREDENTIAL_SECRET_ID"),
        st.sampled_from([None, "", "   "]),
        st.just("credential_secret_arn"),
    ),
    st.tuples(
        st.just("SCM_PROVIDER_BASE_URL"),
        st.sampled_from(["notaurl", "http://insecure.example.com", "ftp://x.example.com", "https://"]),
        st.just("provider_base_url"),
    ),
)


@st.composite
def _scenarios(draw) -> dict:
    """Generate a full V1 truth-table row.

    Independently toggles: the enablement flag (truthy vs non-truthy casings) and the
    validity of each of the three contracts (valid, or invalid via exactly one broken
    field). Returns the settings ``values`` dict, whether the flag is truthy, and a mapping
    of invalid contract name -> expected error substring.
    """
    flag_is_truthy = draw(st.booleans())
    flag = draw(_truthy_flags if flag_is_truthy else _non_truthy_flags)

    domain_inv = draw(st.one_of(st.none(), _domain_invalidations))
    connector_inv = draw(st.one_of(st.none(), _connector_invalidations))
    adapter_inv = draw(st.one_of(st.none(), _adapter_invalidations))

    values = _valid_base()
    values["SCM_CONNECTOR_ENABLED"] = flag

    invalid: dict[str, str] = {}
    for name, inv in (("domain", domain_inv), ("connector", connector_inv), ("adapter", adapter_inv)):
        if inv is not None:
            key, bad_value, substring = inv
            values[key] = bad_value
            invalid[name] = substring

    return {"values": values, "flag_is_truthy": flag_is_truthy, "invalid": invalid}


# --- Property V1 ------------------------------------------------------------


# Feature: source-control-connector-v2, Property V1: config separation governs a single fail-closed enablement gate
@settings(max_examples=100)
@given(scenario=_scenarios())
def test_property_v1_config_separation_single_failclosed_gate(scenario):
    """Domain/config separation governs a single fail-closed enablement gate.

    ``load()`` splits config into DomainConfig / ConnectorConfig / AdapterConfig and resolves
    ``enabled == True`` iff the flag is truthy AND all three contracts validate; any invalid
    contract → disabled, offending values recorded on the owning contract's ``config_errors``,
    a single configuration-error audit entry, and each field homed on exactly its owning
    contract (Req 1.1, 1.3, 1.4, 1.5, 1.6, 10.1, 10.2, 10.3, 10.4).
    """
    values = scenario["values"]
    truthy = scenario["flag_is_truthy"]
    invalid = scenario["invalid"]

    with _scm_settings(values), patch.object(config_module, "logger", MagicMock()) as mock_logger:
        cfg = SourceControlConfig.load()

    # --- Field ownership: each configuration field resides on exactly its owning contract,
    # with no leakage across the three-way split (Req 10.1, 10.2, 10.3, 10.4). ---
    assert {f.name for f in dataclasses.fields(DomainConfig)} == _DOMAIN_FIELDS
    assert {f.name for f in dataclasses.fields(ConnectorConfig)} == _CONNECTOR_FIELDS
    assert {f.name for f in dataclasses.fields(AdapterConfig)} == _ADAPTER_FIELDS
    # The composed config always exposes the three split contracts.
    assert isinstance(cfg.domain, DomainConfig)
    assert isinstance(cfg.connector, ConnectorConfig)
    assert isinstance(cfg.adapter, AdapterConfig)

    all_valid = not invalid
    expected_enabled = truthy and all_valid

    # --- Single fail-closed gate: enabled IFF flag truthy AND all three contracts valid. ---
    assert cfg.enabled is expected_enabled

    if not truthy:
        # Normal off state: disabled, no errors on any contract, no audit entry emitted
        # (validation is short-circuited), so no specialist is registered (Req 1.1, 1.5).
        assert cfg.config_errors == ()
        assert cfg.domain.config_errors == ()
        assert cfg.connector.config_errors == ()
        assert cfg.adapter.config_errors == ()
        mock_logger.error.assert_not_called()
        return

    if all_valid:
        # Truthy flag + all three contracts valid → enabled, clean, no audit entry
        # (Req 1.4, 10.4).
        assert cfg.enabled is True
        assert cfg.config_errors == ()
        mock_logger.error.assert_not_called()
        return

    # Truthy flag + at least one invalid contract → disabled (fail-closed); the offending
    # values are recorded on the owning contract's config_errors, and exactly one sanitized
    # configuration-error audit entry is emitted (Req 1.6, 10.x).
    assert cfg.enabled is False
    contract_errors = {
        "domain": cfg.domain.config_errors,
        "connector": cfg.connector.config_errors,
        "adapter": cfg.adapter.config_errors,
    }
    for name, substring in invalid.items():
        assert contract_errors[name], f"{name} contract must record the offending field"
        assert any(
            substring in err for err in contract_errors[name]
        ), f"{name} error should name '{substring}': {contract_errors[name]}"
    # Contracts left valid record no error, so blame stays on the offending contract only.
    for name in ("domain", "connector", "adapter"):
        if name not in invalid:
            assert contract_errors[name] == ()
    # A single configuration-error audit entry is emitted on the truthy-but-invalid path.
    mock_logger.error.assert_called_once()
