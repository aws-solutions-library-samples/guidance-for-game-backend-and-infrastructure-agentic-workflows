#!/usr/bin/env python3
"""Property-based tests for the connector enablement gate (`connector/config.py`).

Covers Correctness Property 1 from the source-control-connector design: a truthy
enablement flag combined with an otherwise fully-valid configuration enables the
connector. ``ConnectorConfig.load()`` is the single decision point for whether the
Source Control Connector is on, so proving that valid config yields
``enabled == True`` with empty ``config_errors`` and correctly parsed fields is what
makes "opt-in enablement" trustworthy.

Config is driven by monkeypatching the ``GBAW_SCM_*`` values on ``config.settings``
(the module ``connector/config.py`` reads from). ``load()`` performs no secret
retrieval on the enablement path, so no ``get_secret`` mock is required here.

Validates: Requirements 1.4, 8.3
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from config import settings as app_settings
from connector.config import AllowlistEntry, SourceControlConfig

pytestmark = pytest.mark.unit


# --- Hypothesis strategies -------------------------------------------------


@st.composite
def _truthy_flags(draw) -> str:
    """A value that case-insensitively (whitespace-trimmed) matches an accepted true.

    The accepted set is ``{"true", "1", "yes"}``; we randomize the casing of the
    letters and pad with arbitrary surrounding whitespace to exercise the trim +
    case-insensitive match (Req 1.4).
    """
    base = draw(st.sampled_from(["true", "1", "yes"]))
    # Randomize case per character (a no-op for the digit "1").
    cased = "".join(draw(st.sampled_from([ch.lower(), ch.upper()])) for ch in base)
    ws = st.text(alphabet=" \t\n", max_size=3)
    return f"{draw(ws)}{cased}{draw(ws)}"


# Provider name: must name a provider that has a registered adapter, since enablement now
# fails closed via the provider registry (registry.is_supported) — a valid config for an
# unregistered provider is intentionally disabled (Req 7.1, 7.2; hardening Property H2).
# "github" is the bundled, self-registered adapter, so it is the supported provider here.
_providers = st.just("github")


# A valid Secrets Manager ARN, matching the module's _SECRET_ARN_RE. After the v2 single-ARN
# consolidation (MR5) this is the ONLY accepted shape for the credential setting: the
# adapter validates ``GBAW_SCM_CREDENTIAL_SECRET_ARN`` is ARN-shaped when enabled, so a bare
# secret name no longer produces a valid, enabled config.
_secret_arns = st.from_regex(
    r"arn:aws:secretsmanager:[a-z]{2}-[a-z]{4,9}-[0-9]:[0-9]{12}:secret:[A-Za-z0-9/_-]{1,20}",
    fullmatch=True,
)

_credential_secret_arns = _secret_arns


# Repository and branch identifiers that survive the allowlist grammar unchanged: no
# delimiter characters (";", "=", ","), no surrounding whitespace.
_repos = st.from_regex(r"[A-Za-z0-9._-]{1,15}/[A-Za-z0-9._-]{1,15}", fullmatch=True)
_branches = st.from_regex(r"[A-Za-z0-9._/-]{1,15}", fullmatch=True)

# Cognito group names: whitespace-free, comma-free tokens.
_groups = st.from_regex(r"[A-Za-z0-9._-]{1,20}", fullmatch=True)

# Audit log group name: a dedicated CloudWatch Logs log group, required when enabled
# (Req 13.1). Whitespace-free token so it survives load() unchanged.
_audit_log_groups = st.from_regex(r"[A-Za-z0-9._/-]{1,40}", fullmatch=True)

# Provider base URL: either unset (adapter uses the public endpoint) or an absolute HTTPS
# URL (Req 10.2, 10.3). Both keep the connector enabled.
_provider_base_urls = st.one_of(
    st.none(),
    st.from_regex(r"https://[a-z0-9.-]{1,20}\.example\.com(/[A-Za-z0-9._/-]{0,15})?", fullmatch=True),
)


@st.composite
def _allowlist_mappings(draw):
    """A repository -> non-empty branch-set mapping and its grammar encoding.

    Returns ``(expected_entries, encoded)`` where ``expected_entries`` is the tuple of
    :class:`AllowlistEntry` the parser must reproduce and ``encoded`` is the
    ``GBAW_SCM_REPO_ALLOWLIST`` string (Req 5.1).
    """
    repos = draw(st.lists(_repos, min_size=1, max_size=4, unique=True))
    entries: list[AllowlistEntry] = []
    segments: list[str] = []
    for repo in repos:
        branches = draw(st.lists(_branches, min_size=1, max_size=3, unique=True))
        entries.append(AllowlistEntry(repo=repo, target_branches=tuple(branches)))
        segments.append(f"{repo}=" + ",".join(branches))
    return tuple(entries), ";".join(segments)


@st.composite
def _valid_configs(draw) -> dict:
    """A complete, valid ``GBAW_SCM_*`` configuration as a dict of string env values.

    Every required field is present and every numeric value is inside its permitted
    range, so ``load()`` must return an enabled connector (Req 1.4, 8.3).
    """
    expected_entries, allowlist_encoded = draw(_allowlist_mappings())
    groups = draw(st.lists(_groups, min_size=1, max_size=4, unique=True))

    rate_limit_max = draw(st.integers(min_value=1, max_value=1000))
    rate_limit_window = draw(st.integers(min_value=60, max_value=86400))
    provider_timeout = draw(st.integers(min_value=1, max_value=300))
    retry_max = draw(st.integers(min_value=1, max_value=10))
    max_files = draw(st.integers(min_value=1, max_value=1000))

    return {
        "flag": draw(_truthy_flags()),
        "provider": draw(_providers),
        "provider_base_url": draw(_provider_base_urls),
        "credential_secret_arn": draw(_credential_secret_arns),
        "allowlist_encoded": allowlist_encoded,
        "expected_entries": expected_entries,
        "groups": groups,
        "audit_log_group": draw(_audit_log_groups),
        "rate_limit_max": rate_limit_max,
        "rate_limit_window": rate_limit_window,
        "provider_timeout": provider_timeout,
        "retry_max": retry_max,
        "max_files": max_files,
    }


def _patched_settings(cfg: dict):
    """Return a context manager that patches the ``GBAW_SCM_*`` values on settings.

    A context manager (rather than the function-scoped ``monkeypatch`` fixture) is used
    so the patched values are applied and restored *per generated example*.
    """
    return mock.patch.multiple(
        app_settings,
        SCM_CONNECTOR_ENABLED=cfg["flag"],
        SCM_PROVIDER=cfg["provider"],
        SCM_PROVIDER_BASE_URL=cfg["provider_base_url"],
        SCM_READ_CREDENTIAL_SECRET_ARN=cfg["credential_secret_arn"],
        SCM_REPO_ALLOWLIST=cfg["allowlist_encoded"],
        SCM_AUTHORIZED_GROUPS=",".join(cfg["groups"]),
        SCM_AUDIT_LOG_GROUP=cfg["audit_log_group"],
        SCM_RATE_LIMIT_MAX=str(cfg["rate_limit_max"]),
        SCM_RATE_LIMIT_WINDOW_SECONDS=str(cfg["rate_limit_window"]),
        SCM_PROVIDER_TIMEOUT_SECONDS=str(cfg["provider_timeout"]),
        SCM_RETRY_MAX_ATTEMPTS=str(cfg["retry_max"]),
        SCM_MAX_FILES_PER_REQUEST=str(cfg["max_files"]),
    )


# --- Property 1 ------------------------------------------------------------


# Feature: source-control-connector, Property 1: Truthy + valid config enables the connector
@settings(max_examples=100)
@given(cfg=_valid_configs())
def test_property1_truthy_valid_config_enables_connector(cfg):
    """A truthy flag + otherwise-valid config yields enabled=True, no errors.

    Also confirms every required field is parsed correctly, since "enabled" is only
    meaningful if the connector acts on the operator's intended values (Req 1.4, 8.3).
    """
    with _patched_settings(cfg):
        config = SourceControlConfig.load()

    # Core property: valid config enables the connector with no accumulated errors.
    assert config.enabled is True
    assert config.config_errors == ()

    # Fields are parsed exactly as configured, each on its owning split contract
    # (values are whitespace-free by construction).
    assert config.connector.provider == cfg["provider"]
    assert config.adapter.provider_base_url == cfg["provider_base_url"]
    assert config.connector.audit_log_group == cfg["audit_log_group"]
    assert config.adapter.read_credential_secret_arn == cfg["credential_secret_arn"]
    assert config.domain.authorization_policy == cfg["expected_entries"]
    assert config.domain.authorized_groups == tuple(cfg["groups"])
    assert config.connector.rate_limit_max == cfg["rate_limit_max"]
    assert config.connector.rate_limit_window_seconds == cfg["rate_limit_window"]
    assert config.connector.provider_timeout_seconds == cfg["provider_timeout"]
    assert config.connector.retry_max_attempts == cfg["retry_max"]
    assert config.connector.max_files_per_request == cfg["max_files"]
