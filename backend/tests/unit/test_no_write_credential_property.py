#!/usr/bin/env python3
"""Property-based test: the chat runtime holds no write credential.

# Feature: source-control-connector-readonly-split, Property 2: the chat runtime holds no write credential

This is a non-optional MR security-posture property. For all resolved runtime
configurations of the read-only connector, the configuration exposes **no** write-credential
field and references **no** write-credential ARN — the only credential reference present is
the read-credential ARN (``AdapterConfig.read_credential_secret_arn``).

The property is checked two ways:

1. **Structural** — introspecting the fields of the three composed contracts
   (:class:`DomainConfig`, :class:`ConnectorConfig`, :class:`AdapterConfig`) plus the
   composed :class:`SourceControlConfig`, the only credential-named field across the whole
   configuration surface is ``read_credential_secret_arn``, and the ``config.settings``
   module exposes no write-credential input variable.
2. **Value** — for a generated read ARN (and a distinct decoy *write* ARN that is never
   supplied anywhere), the resolved config surfaces the read ARN at
   ``adapter.read_credential_secret_arn`` and the decoy write ARN appears in no field value.

Validates: Requirements 3.2, 4.4, 6.5
"""

# Standard library
import dataclasses

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from config import settings as app_settings
from connector.config import (
    AdapterConfig,
    ConnectorConfig,
    DomainConfig,
    SourceControlConfig,
)
from support.config_factory import make_source_control_config

pytestmark = pytest.mark.unit

# The single credential reference the read-only connector is permitted to hold.
_READ_CREDENTIAL_FIELD = "read_credential_secret_arn"

# Field/attribute name fragments that would betray a retained write-credential surface.
_WRITE_CREDENTIAL_INPUTS = (
    "SCM_CREDENTIAL_SECRET_ARN",
    "SCM_CREDENTIAL_SECRET_ID",
    "SCM_WRITE_CREDENTIAL_SECRET_ARN",
)

# A Secrets Manager ARN strategy, used for both the (legitimate) read ARN and a distinct
# decoy write ARN that must never appear anywhere in a resolved config.
_arns = st.from_regex(
    r"arn:aws:secretsmanager:us-west-2:123456789012:secret:[A-Za-z0-9/_-]{4,20}-[A-Za-z0-9]{6}",
    fullmatch=True,
)


def _credential_field_names() -> set[str]:
    """Return every credential-named field across all connector config contracts."""
    names: set[str] = set()
    for contract in (DomainConfig, ConnectorConfig, AdapterConfig, SourceControlConfig):
        for f in dataclasses.fields(contract):
            if "credential" in f.name.lower():
                names.add(f.name)
    return names


def _all_string_values(config: SourceControlConfig) -> list[str]:
    """Flatten every string value reachable from the composed config's contract fields."""
    values: list[str] = []
    for contract in (config.domain, config.connector, config.adapter):
        for f in dataclasses.fields(contract):
            value = getattr(contract, f.name)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, (list, tuple)):
                values.extend(item for item in value if isinstance(item, str))
    return values


def test_config_surface_has_only_the_read_credential_field():
    """The only credential-named field is the read ARN, and no write-credential input exists.

    A one-shot structural check (Req 3.2, 4.4, 6.5): across all three contracts and the
    composed config there is exactly one credential field — ``read_credential_secret_arn`` —
    and the settings module carries no write-credential input variable.
    """
    assert _credential_field_names() == {_READ_CREDENTIAL_FIELD}

    for write_input in _WRITE_CREDENTIAL_INPUTS:
        assert not hasattr(app_settings, write_input), f"settings still exposes {write_input}"
    # The read-credential input is the only SCM credential variable present.
    assert hasattr(app_settings, "SCM_READ_CREDENTIAL_SECRET_ARN")


# Feature: source-control-connector-readonly-split, Property 2: the chat runtime holds no write credential
@settings(max_examples=100)
@given(read_arn=_arns, decoy_write_arn=_arns)
def test_property2_resolved_config_references_only_the_read_arn(read_arn, decoy_write_arn):
    """A resolved config surfaces only the read ARN; a decoy write ARN appears nowhere.

    For all resolved runtime configurations, the read credential is the sole credential
    reference: it is surfaced at ``adapter.read_credential_secret_arn`` and a distinct write
    ARN — never supplied to the config — is present in no field value (Req 3.2, 4.4, 6.5).
    """
    # Ensure the decoy write ARN is genuinely distinct so its absence is a real check.
    if decoy_write_arn == read_arn:
        decoy_write_arn = decoy_write_arn + "-decoy"

    config = make_source_control_config(
        enabled=True,
        provider="github",
        read_credential_secret_arn=read_arn,
        allowlist=(),
        authorized_groups=("iac-admins",),
        audit_log_group="scm-audit",
    )

    # The only credential reference present is the read ARN.
    assert config.adapter.read_credential_secret_arn == read_arn

    string_values = _all_string_values(config)
    # The read ARN is present exactly (at the read-credential field) ...
    assert read_arn in string_values
    # ... and the write ARN — never supplied — is referenced nowhere.
    assert decoy_write_arn not in string_values

    # No write-credential field exists to hold it in the first place.
    assert _credential_field_names() == {_READ_CREDENTIAL_FIELD}
