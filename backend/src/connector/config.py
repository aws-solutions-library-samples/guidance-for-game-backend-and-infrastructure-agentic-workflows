"""Connector configuration, validation, and the fail-closed enablement gate.

This module turns the raw ``GBAW_SCM_*`` environment values parsed in
``config/settings.py`` into three frozen, validated configuration contracts composed by a
:class:`SourceControlConfig`. It is the single decision point for whether the Source
Control Connector is *enabled*.

Design contract (see ``.kiro/specs/source-control-connector-v2/design.md`` → Component 1
and the Data Models "field move" table). The v2 pass splits the previously monolithic
``ConnectorConfig`` into three cohesive layers, each owning one contract:

- :class:`DomainConfig` (IaC_Change_Domain) owns the authorization policy (the repository
  allowlist) and the authorized Cognito groups.
- :class:`ConnectorConfig` (Connector_Core, provider-neutral) owns operational tuning:
  ``provider``, rate limiting, provider timeout, retry attempts, max files per request, and
  the audit destination log group.
- :class:`AdapterConfig` (Provider_Adapter) owns the credential secret ARN and the optional
  provider base URL.

Composition happens in :meth:`SourceControlConfig.load`, which builds all three contracts
from the ``GBAW_SCM_*`` values and resolves a single ``enabled`` gate that is truthy **iff
the enablement flag is truthy AND all three contracts validate**. Behavior that predates
the split is preserved exactly, only re-homed:

- ``load()`` reads connector configuration **exclusively** from the ``GBAW_``-prefixed
  values exposed by ``config/settings.py`` and ignores every other source (Req 12.1).
- Enablement is truthy only when the flag case-insensitively (whitespace-trimmed) matches
  one of ``{"true", "1", "yes"}`` (Req 1.4, 1.5).
- Each contract **accumulates every validation failure** into its own ``config_errors``
  rather than raising; any non-empty ``config_errors`` on any contract forces
  ``enabled=False`` — misconfiguration yields a disabled connector plus an audit entry,
  never an import-time crash (Req 1.6, 12.4).
- When the flag is truthy but a required value is missing/invalid, a single
  configuration-error audit entry is emitted via the existing ``logger`` with every field
  passed through ``sanitize_log_data`` so no raw credential can leak (Req 1.6, 12.3, 12.4).
- The credential is referenced **only** by a single Secrets Manager secret ARN
  (``GBAW_SCM_CREDENTIAL_SECRET_ARN``); a value that is not ARN-shaped (a bare secret name,
  or a raw credential accidentally supplied here) is rejected and its value is excluded from
  all audit output (Req 11.2, 12.3).

Per-contract validation rules (all failures accumulate on the owning contract):

- Enablement flag not truthy → disabled, **no error** (normal off state) (Req 1.1, 1.5).
- ``provider`` unset/empty → error (Req 9.5); a provider with no registered adapter (per
  ``connector.registry.is_supported``) → error and disabled (Req 7.1, 7.2).
- ``provider_base_url`` set but not an absolute https URL → error and disabled; unset →
  ``None`` so the adapter uses the provider's public endpoint (Req 10.2, 10.3, 10.4).
- ``audit_log_group`` absent on the enabled path → error and disabled (Req 13.1).
- ``credential_secret_arn`` unset → error; value not ARN-shaped → error and the value is
  omitted from audit output (Req 11.2, 12.3).
- Allowlist unparsable or zero entries → error (Req 5.4).
- ``authorized_groups`` empty → error (Req 7.5).
- ``rate_limit_max`` outside 1..1000 or ``rate_limit_window_seconds`` outside 60..86400 →
  error; absent → defaults 5 / 3600 (Req 8.3, 8.4, 8.5).
- ``provider_timeout_seconds`` outside 1..300 → error; absent → 30 (Req 10.1).
- ``retry_max_attempts`` outside 1..10 → error; absent → 3 (Req 10.5).
- ``max_files_per_request`` not a positive integer → error; absent → 20 (Req 3.2).
"""

# Standard library
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# Local modules
from config import settings
from connector import registry
from utils.logger import logger
from utils.security import sanitize_log_data

__all__ = [
    "AllowlistEntry",
    "DomainConfig",
    "ConnectorConfig",
    "AdapterConfig",
    "SourceControlConfig",
]

# Accepted truthy enablement values (compared case-insensitively, whitespace-trimmed).
_TRUTHY_VALUES = frozenset({"true", "1", "yes"})

# Permitted ranges and defaults for the numeric tuning values.
_RATE_LIMIT_MAX_MIN, _RATE_LIMIT_MAX_MAX, _RATE_LIMIT_MAX_DEFAULT = 1, 1000, 5
_RATE_WINDOW_MIN, _RATE_WINDOW_MAX, _RATE_WINDOW_DEFAULT = 60, 86400, 3600
_TIMEOUT_MIN, _TIMEOUT_MAX, _TIMEOUT_DEFAULT = 1, 300, 30
_RETRY_MIN, _RETRY_MAX, _RETRY_DEFAULT = 1, 10, 3
_MAX_FILES_DEFAULT = 20

# A Secrets Manager secret ARN, e.g.
# arn:aws:secretsmanager:us-west-2:123456789012:secret:my/secret-AbCdEf
# This is the single ARN-valued credential setting's required shape (Req 11.2 / MR5).
_SECRET_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:.+$"
)


@dataclass(frozen=True)
class AllowlistEntry:
    """One repository paired with the branches the Connector may target (Req 5.1).

    ``repo`` is an exact repository identifier (e.g. ``"org/iac-repo"``) and
    ``target_branches`` is one or more exact branch names. Comparison at the tool
    boundary is case-sensitive, full-string (Req 5.2).
    """

    repo: str
    target_branches: tuple[str, ...]


@dataclass(frozen=True)
class DomainConfig:
    """IaC_Change_Domain configuration: authorization policy + authorized groups.

    Owns the operator-approved repository ``authorization_policy`` (the allowlist) and the
    ``authorized_groups`` a requesting user must belong to. This task keeps the policy
    minimal — it holds the existing tuple of :class:`AllowlistEntry` (repo + branch) and the
    authorized groups exactly as before the split; the path/extension dimensions are added
    by a later v2 task. ``config_errors`` accumulates this contract's validation failures.
    """

    authorization_policy: tuple[AllowlistEntry, ...]
    authorized_groups: tuple[str, ...]
    config_errors: tuple[str, ...]

    @classmethod
    def load(cls) -> "DomainConfig":
        """Build the domain contract from ``GBAW_SCM_*`` values, accumulating errors."""
        errors: list[str] = []

        # --- Repository allowlist: must parse to at least one entry (Req 5.1, 5.4). ---
        allowlist, allowlist_errors = _parse_allowlist(settings.SCM_REPO_ALLOWLIST)
        errors.extend(allowlist_errors)
        if not allowlist and not allowlist_errors:
            errors.append(
                "allowlist: GBAW_SCM_REPO_ALLOWLIST is required and must contain at least one entry"
            )

        # --- Authorized groups: comma-separated, at least one non-empty (Req 7.5). ---
        authorized_groups = tuple(
            g.strip() for g in (settings.SCM_AUTHORIZED_GROUPS or "").split(",") if g.strip()
        )
        if not authorized_groups:
            errors.append(
                "authorized_groups: GBAW_SCM_AUTHORIZED_GROUPS is required and must list at "
                "least one Cognito group"
            )

        return cls(
            authorization_policy=allowlist,
            authorized_groups=authorized_groups,
            config_errors=tuple(errors),
        )

    @classmethod
    def _empty(cls) -> "DomainConfig":
        """Return the well-formed empty domain contract for the disabled off state."""
        return cls(authorization_policy=(), authorized_groups=(), config_errors=())


@dataclass(frozen=True)
class ConnectorConfig:
    """Connector_Core (provider-neutral) operational tuning contract.

    Owns the ``provider`` name, rate-limit window, provider timeout, retry attempts, the
    per-request file cap, and the durable audit destination log group. Numeric fields always
    hold a valid in-range value (falling back to the documented default when the supplied
    value was absent or invalid) so downstream code can rely on them regardless of the
    enablement outcome. ``config_errors`` accumulates this contract's validation failures.
    """

    provider: str | None
    rate_limit_max: int
    rate_limit_window_seconds: int
    provider_timeout_seconds: int
    retry_max_attempts: int
    max_files_per_request: int
    audit_log_group: str | None
    config_errors: tuple[str, ...]

    @classmethod
    def load(cls) -> "ConnectorConfig":
        """Build the neutral core contract from ``GBAW_SCM_*`` values, accumulating errors."""
        errors: list[str] = []

        # --- Provider: present/non-empty AND backed by a registered adapter. The registry
        # is the single source of truth for which providers are supported; enablement fails
        # closed when no adapter is registered for the configured provider (Req 7.1, 7.2). ---
        provider = (settings.SCM_PROVIDER or "").strip() or None
        if provider is None:
            errors.append("provider: GBAW_SCM_PROVIDER is required but was not set")
        elif not registry.is_supported(provider):
            errors.append(
                f"provider: no source-control adapter is registered for provider '{provider}'; "
                "connector disabled (fail-closed)"
            )

        # --- Audit log group: required when the connector is enabled, since the durable
        # audit sink needs a target CloudWatch Logs group. Absent/empty on the enabled path
        # accumulates a config error and forces the connector disabled (Req 13.1). ---
        audit_log_group = (settings.SCM_AUDIT_LOG_GROUP or "").strip() or None
        if audit_log_group is None:
            errors.append(
                "audit_log_group: GBAW_SCM_AUDIT_LOG_GROUP is required when the connector is "
                "enabled but was not set"
            )

        # --- Numeric tuning values: parse + range-check, falling back to defaults. ---
        rate_limit_max = _parse_ranged_int(
            settings.SCM_RATE_LIMIT_MAX,
            name="rate_limit_max",
            env="GBAW_SCM_RATE_LIMIT_MAX",
            minimum=_RATE_LIMIT_MAX_MIN,
            maximum=_RATE_LIMIT_MAX_MAX,
            default=_RATE_LIMIT_MAX_DEFAULT,
            errors=errors,
        )
        rate_limit_window_seconds = _parse_ranged_int(
            settings.SCM_RATE_LIMIT_WINDOW_SECONDS,
            name="rate_limit_window_seconds",
            env="GBAW_SCM_RATE_LIMIT_WINDOW_SECONDS",
            minimum=_RATE_WINDOW_MIN,
            maximum=_RATE_WINDOW_MAX,
            default=_RATE_WINDOW_DEFAULT,
            errors=errors,
        )
        provider_timeout_seconds = _parse_ranged_int(
            settings.SCM_PROVIDER_TIMEOUT_SECONDS,
            name="provider_timeout_seconds",
            env="GBAW_SCM_PROVIDER_TIMEOUT_SECONDS",
            minimum=_TIMEOUT_MIN,
            maximum=_TIMEOUT_MAX,
            default=_TIMEOUT_DEFAULT,
            errors=errors,
        )
        retry_max_attempts = _parse_ranged_int(
            settings.SCM_RETRY_MAX_ATTEMPTS,
            name="retry_max_attempts",
            env="GBAW_SCM_RETRY_MAX_ATTEMPTS",
            minimum=_RETRY_MIN,
            maximum=_RETRY_MAX,
            default=_RETRY_DEFAULT,
            errors=errors,
        )
        max_files_per_request = _parse_ranged_int(
            settings.SCM_MAX_FILES_PER_REQUEST,
            name="max_files_per_request",
            env="GBAW_SCM_MAX_FILES_PER_REQUEST",
            minimum=1,
            maximum=None,
            default=_MAX_FILES_DEFAULT,
            errors=errors,
        )

        return cls(
            provider=provider,
            rate_limit_max=rate_limit_max,
            rate_limit_window_seconds=rate_limit_window_seconds,
            provider_timeout_seconds=provider_timeout_seconds,
            retry_max_attempts=retry_max_attempts,
            max_files_per_request=max_files_per_request,
            audit_log_group=audit_log_group,
            config_errors=tuple(errors),
        )

    @classmethod
    def _defaults(cls) -> "ConnectorConfig":
        """Return the neutral core contract with documented defaults (disabled off state)."""
        return cls(
            provider=None,
            rate_limit_max=_RATE_LIMIT_MAX_DEFAULT,
            rate_limit_window_seconds=_RATE_WINDOW_DEFAULT,
            provider_timeout_seconds=_TIMEOUT_DEFAULT,
            retry_max_attempts=_RETRY_DEFAULT,
            max_files_per_request=_MAX_FILES_DEFAULT,
            audit_log_group=None,
            config_errors=(),
        )


@dataclass(frozen=True)
class AdapterConfig:
    """Provider_Adapter configuration: credential secret ARN + provider base URL.

    Owns the credential reference (``credential_secret_arn``) the adapter uses to acquire
    provider credentials and the optional ``provider_base_url`` for self-hosted/enterprise
    endpoints. ``config_errors`` accumulates this contract's validation failures.

    The credential is the single ARN-valued setting sourced from
    ``GBAW_SCM_CREDENTIAL_SECRET_ARN`` (the v2 single-ARN consolidation, MR5). The same ARN
    is used for both runtime acquisition — the adapter's :class:`ProviderAuth` fetches the
    secret at this ARN — and the scoped IAM grant, so runtime config and IAM scope cannot
    drift. When the connector is enabled the value MUST be ARN-shaped; any other value
    (including a raw credential accidentally supplied in its place) fails closed with a
    config error and is never echoed into the error/audit output (Req 11.2, 12.3).
    """

    credential_secret_arn: str | None
    provider_base_url: str | None
    config_errors: tuple[str, ...]

    @classmethod
    def load(cls) -> "AdapterConfig":
        """Build the adapter contract from ``GBAW_SCM_*`` values, accumulating errors."""
        errors: list[str] = []

        # --- Credential secret ARN: required, and must be a Secrets Manager ARN. A value
        # that is not ARN-shaped (a bare secret name, or a raw credential accidentally
        # supplied here) is rejected fail-closed; the value is NEVER echoed into the error
        # or audit output in case it is a raw credential (Req 11.2, 12.3).
        raw_secret = (settings.SCM_CREDENTIAL_SECRET_ARN or "").strip()
        credential_secret_arn: str | None = raw_secret or None
        if credential_secret_arn is None:
            errors.append(
                "credential_secret_arn: GBAW_SCM_CREDENTIAL_SECRET_ARN is required but was not set"
            )
        elif not _SECRET_ARN_RE.match(credential_secret_arn):
            # Reject and NEVER echo the value into the error/audit output.
            credential_secret_arn = None
            errors.append(
                "credential_secret_arn: GBAW_SCM_CREDENTIAL_SECRET_ARN must be a valid AWS "
                "Secrets Manager secret ARN; value rejected and omitted"
            )

        # --- Provider base URL: optional. When set it MUST be an absolute HTTPS URL
        # (scheme "https" + non-empty host); an invalid value fails closed with a config
        # error and disables the connector. When unset the adapter defaults to the
        # provider's public endpoint (Req 10.1, 10.2, 10.3, 10.4). ---
        raw_base_url = (settings.SCM_PROVIDER_BASE_URL or "").strip()
        provider_base_url: str | None = raw_base_url or None
        if provider_base_url is not None and not _is_absolute_https_url(provider_base_url):
            errors.append(
                f"provider_base_url: GBAW_SCM_PROVIDER_BASE_URL value '{provider_base_url}' is "
                "not a valid absolute https URL"
            )
            provider_base_url = None

        return cls(
            credential_secret_arn=credential_secret_arn,
            provider_base_url=provider_base_url,
            config_errors=tuple(errors),
        )

    @classmethod
    def _empty(cls) -> "AdapterConfig":
        """Return the well-formed empty adapter contract for the disabled off state."""
        return cls(credential_secret_arn=None, provider_base_url=None, config_errors=())


@dataclass(frozen=True)
class SourceControlConfig:
    """Composed connector configuration and the single fail-closed ``enabled`` decision.

    Composes the three layer contracts (:class:`DomainConfig`, :class:`ConnectorConfig`,
    :class:`AdapterConfig`) and resolves one ``enabled`` gate. ``enabled`` is ``True`` iff
    the enablement flag is truthy AND all three contracts validate; any contract error
    forces ``enabled=False`` (Req 1.6, 12.4).
    """

    enabled: bool
    domain: DomainConfig
    connector: ConnectorConfig
    adapter: AdapterConfig

    @property
    def config_errors(self) -> tuple[str, ...]:
        """Aggregate every contract's accumulated configuration errors, in layer order."""
        return (
            self.domain.config_errors
            + self.connector.config_errors
            + self.adapter.config_errors
        )

    @classmethod
    def load(cls) -> "SourceControlConfig":
        """Read ``GBAW_SCM_*`` config, build the three contracts, and resolve ``enabled``.

        Reads exclusively from the ``GBAW_``-prefixed values on ``config.settings``
        (Req 12.1). Never raises: every failure is accumulated into the owning contract's
        ``config_errors`` and forces ``enabled=False``. When the enablement flag is truthy
        but validation fails, a single configuration-error audit entry is emitted across all
        accumulated errors (Req 1.6, 12.4).
        """
        raw_flag = (settings.SCM_CONNECTOR_ENABLED or "").strip().lower()
        truthy = raw_flag in _TRUTHY_VALUES

        # Not truthy is the normal off state: the connector is disabled with NO error and
        # no audit entry (Req 1.1, 1.5). Short-circuit before validation so a default
        # read-only deployment reports empty config_errors.
        if not truthy:
            return cls._disabled_off_state()

        domain = DomainConfig.load()
        connector = ConnectorConfig.load()
        adapter = AdapterConfig.load()

        # Enablement gate: the flag is truthy here (non-truthy short-circuited above), so
        # enablement hinges solely on whether any contract accumulated errors (Req 1.4,
        # 1.6). The operator asked for the connector but validation failed → emit a single
        # configuration-error audit entry across all contracts (Req 1.6, 12.4).
        all_errors = list(
            domain.config_errors + connector.config_errors + adapter.config_errors
        )
        enabled = not all_errors
        if all_errors:
            _emit_config_error_audit(all_errors)

        return cls(
            enabled=enabled,
            domain=domain,
            connector=connector,
            adapter=adapter,
        )

    @classmethod
    def _disabled_off_state(cls) -> "SourceControlConfig":
        """Return the disabled config for the normal off state (flag not truthy).

        No validation is performed and no error/audit is produced (Req 1.1, 1.5); each
        contract carries its documented defaults so the object is always well-formed.
        """
        return cls(
            enabled=False,
            domain=DomainConfig._empty(),
            connector=ConnectorConfig._defaults(),
            adapter=AdapterConfig._empty(),
        )


def _is_absolute_https_url(value: str) -> bool:
    """Return ``True`` if ``value`` is an absolute HTTPS URL (Req 10.4).

    Requires the ``https`` scheme and a non-empty network location (host). Any parse
    failure, a non-``https`` scheme, or a missing host makes the value invalid so the
    connector fails closed on a misconfigured Provider_Base_URL.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _parse_allowlist(raw: str | None) -> tuple[tuple[AllowlistEntry, ...], list[str]]:
    """Parse the ``GBAW_SCM_REPO_ALLOWLIST`` grammar into ``AllowlistEntry`` values.

    Grammar (Req 5.1)::

        allowlist := entry ( ";" entry )*
        entry     := repo "=" branch ( "," branch )*

    Returns the parsed entries (order preserved) and a list of per-entry parse errors.
    Empty ``;``-separated segments are ignored so a trailing separator is harmless.
    """
    errors: list[str] = []
    if not raw or not raw.strip():
        return (), errors

    entries: list[AllowlistEntry] = []
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            errors.append(
                f"allowlist: entry '{segment}' is malformed (expected 'repo=branch[,branch...]')"
            )
            continue
        repo_part, branches_part = segment.split("=", 1)
        repo = repo_part.strip()
        branches = tuple(b.strip() for b in branches_part.split(",") if b.strip())
        if not repo:
            errors.append(f"allowlist: entry '{segment}' has an empty repository identifier")
            continue
        if not branches:
            errors.append(f"allowlist: repository '{repo}' has no target branches")
            continue
        entries.append(AllowlistEntry(repo=repo, target_branches=branches))

    return tuple(entries), errors


def _parse_ranged_int(
    raw: str | None,
    *,
    name: str,
    env: str,
    minimum: int,
    maximum: int | None,
    default: int,
    errors: list[str],
) -> int:
    """Parse ``raw`` as an int, validate its range, and append an error on failure.

    On any failure (non-integer or out of ``[minimum, maximum]``) the documented
    ``default`` is returned so the frozen config always holds a usable value, while an
    explanatory message is appended to ``errors`` (Req 8.5, 10.1, 10.5).
    """
    text = (raw or "").strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        errors.append(f"{name}: {env} value is not a valid integer")
        return default

    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        errors.append(f"{name}: {env} value {value} is outside the permitted range ({bound})")
        return default

    return value


def _emit_config_error_audit(errors: list[str]) -> None:
    """Emit a single configuration-error audit entry (Req 1.6, 12.4).

    Every error string is passed through ``sanitize_log_data`` as defense-in-depth so no
    sensitive value can leak into the audit log (Req 12.3). The connector remains disabled.
    """
    sanitized = [sanitize_log_data(err) for err in errors]
    logger.error(
        "Source Control Connector configuration error; connector disabled",
        event="scm_config_error",
        outcome="disabled",
        config_errors=sanitized,
    )
