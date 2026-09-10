"""Test helper for building a composed :class:`SourceControlConfig`.

The v2 pass split the previously monolithic connector config into three layer contracts
(``DomainConfig`` / neutral ``ConnectorConfig`` / ``AdapterConfig``) composed by
``SourceControlConfig``. Baseline and hardening tests historically built one flat config
and injected it via ``config=`` into ``connector.service`` entry points. This helper lets
those tests keep passing the same *flat* keyword arguments while producing a correctly
composed :class:`SourceControlConfig`, so the split is a pure structural change with no
behavioral drift in the tests.

The underlying credential setting is now the single ARN-valued read credential
``AdapterConfig.read_credential_secret_arn``. The legacy ``credential_secret_id`` /
``credential_secret_arn`` keywords are still accepted as aliases and mapped onto
``read_credential_secret_arn`` so existing call sites keep working with minimal churn; the
factory builds the config object directly and does not enforce ARN shape, so tests may pass
any sentinel value.
"""

from __future__ import annotations

# Standard library
from collections.abc import Sequence

# Local modules
from connector.config import (
    AdapterConfig,
    AllowlistEntry,
    ConnectorConfig,
    DomainConfig,
    SourceControlConfig,
)


def make_source_control_config(
    *,
    enabled: bool = True,
    provider: str | None = "github",
    credential_secret_id: str | None = None,
    credential_secret_arn: str | None = None,
    read_credential_secret_arn: str | None = None,
    allowlist: Sequence[AllowlistEntry] = (),
    authorized_groups: Sequence[str] = (),
    rate_limit_max: int = 5,
    rate_limit_window_seconds: int = 3600,
    provider_timeout_seconds: int = 30,
    retry_max_attempts: int = 3,
    max_files_per_request: int = 20,
    max_content_bytes: int = 1048576,
    provider_base_url: str | None = None,
    audit_log_group: str | None = None,
    config_errors: Sequence[str] = (),
) -> SourceControlConfig:
    """Build a composed :class:`SourceControlConfig` from flat keyword arguments.

    Field homing mirrors the design's "field move" table: the allowlist
    (``authorization_policy``) and ``authorized_groups`` go to :class:`DomainConfig`; the
    neutral operational tuning and the audit destination go to :class:`ConnectorConfig`; the
    credential ARN and provider base URL go to :class:`AdapterConfig`. ``config_errors`` is
    accepted for call-site compatibility; when supplied it is recorded on the domain
    contract so an aggregate read still observes it.
    """
    arn = read_credential_secret_arn
    if arn is None:
        arn = credential_secret_arn if credential_secret_arn is not None else credential_secret_id

    domain = DomainConfig(
        authorization_policy=tuple(allowlist),
        authorized_groups=tuple(authorized_groups),
        config_errors=tuple(config_errors),
    )
    connector = ConnectorConfig(
        provider=provider,
        rate_limit_max=rate_limit_max,
        rate_limit_window_seconds=rate_limit_window_seconds,
        provider_timeout_seconds=provider_timeout_seconds,
        retry_max_attempts=retry_max_attempts,
        max_files_per_request=max_files_per_request,
        max_content_bytes=max_content_bytes,
        audit_log_group=audit_log_group,
        config_errors=(),
    )
    adapter = AdapterConfig(
        read_credential_secret_arn=arn,
        provider_base_url=provider_base_url,
        config_errors=(),
    )
    return SourceControlConfig(
        enabled=enabled,
        domain=domain,
        connector=connector,
        adapter=adapter,
    )
