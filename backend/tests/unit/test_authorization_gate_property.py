#!/usr/bin/env python3
"""Property-based test for the connector propose-path authorization gate.

Covers Correctness Property 6 from the source-control-connector design: the
authorization gate. ``connector.service.propose_change`` runs a fail-closed safety
pipeline; the authorization gate (Gate 3) is what guarantees that only an
*authenticated* Requesting_User who belongs to at least one operator-configured
authorized group can ever cause a source-control operation.

The property proven here is: a proposal proceeds if and only if the request-context
identity is authenticated (non-empty ``user_id``) AND the user's groups intersect the
configured ``authorized_groups``. Three input families are generated:

  (a) unauthenticated (no ``user_id``)               -> rejected, zero provider calls
  (b) authenticated but no group overlap             -> rejected, zero provider calls
  (c) authenticated with at least one group overlap  -> passes the auth gate and
      (with every later gate satisfied) creates the proposal

Identity is supplied strictly through the request contextvar
(``utils.request_context``), never through model/tool input — ``propose_change`` has
no user parameter, so a prompt-injected model cannot escalate privilege. The provider
is a ``FakeProvider`` injected via ``provider=`` and never touched on a rejected path;
``get_secret`` is mocked so no AWS call occurs; the config carries a known
``authorized_groups`` and an allowlist that the effective repo/branch default to.

Validates: Requirements 7.1, 7.2, 7.3, 7.4
"""

# Standard library
from unittest.mock import patch

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
import utils.security as security
from connector.config import AllowlistEntry, ConnectorConfig
from connector.models import ProposedFile
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixtures for the request that must reach (or be blocked before) the gate ------

# The configured repository/branch the propose path defaults to (allowlist[0]); the
# request supplies no repo/branch so an authorized request lands an exact allowlist
# match and proceeds to create a proposal.
_REPO = "org/iac-repo"
_BRANCH = "main"

# A benign intent/title/description that passes input validation and prompt-injection
# detection (Gate 2) so the authorization gate (Gate 3) is what decides the outcome.
_INTENT = "Update the S3 bucket configuration in the infrastructure template"
_TITLE = "Update bucket configuration"
_DESCRIPTION = "Adjust the bucket settings to match the requested configuration."

# Structurally valid CloudFormation so IaC validation (a later gate) passes for the
# authorized case and does not mask the authorization decision.
_VALID_CFN = "Resources:\n  MyBucket:\n    Type: AWS::S3::Bucket\n"

# A stand-in credential value returned by the mocked get_secret so the credential gate
# passes for the authorized case.
_FAKE_CREDENTIAL = "ghp_faketokenvalue_not_a_real_secret_0000"

# Disjoint group pools: any subset of the authorized pool is disjoint from any subset
# of the "other" pool, so a non-overlap case is guaranteed to have no intersection.
_AUTHORIZED_POOL = ["writers", "admins", "sre", "platform", "infra"]
_OTHER_POOL = ["viewers", "guests", "readonly", "analysts", "auditors"]


def _make_config(authorized_groups: tuple[str, ...]) -> ConnectorConfig:
    """Build an enabled ConnectorConfig with the given ``authorized_groups``.

    ``rate_limit_max`` is set high so the rate-limit gate (which sits after
    authorization) never rejects an authorized request within a run; the rate-limit
    window store is also cleared per example in the test body.
    """
    return ConnectorConfig(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=authorized_groups,
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        config_errors=(),
    )


def _proposed_files() -> list[ProposedFile]:
    return [ProposedFile(path="template.yaml", content=_VALID_CFN, iac_format="cloudformation")]


# --- Hypothesis strategy ----------------------------------------------------


@st.composite
def _auth_scenarios(draw):
    """Generate an authorization scenario across the three input families.

    Returns a dict with the configured ``authorized_groups``, the request-context
    ``user_id``/``groups``, the scenario ``category`` and the expected ``authorized``
    outcome (True only for authenticated-with-overlap).
    """
    authorized_groups = tuple(
        draw(st.lists(st.sampled_from(_AUTHORIZED_POOL), min_size=1, unique=True))
    )
    category = draw(st.sampled_from(["unauthenticated", "no_overlap", "overlap"]))

    if category == "unauthenticated":
        # No authenticated identity: user_id is missing/empty. Groups are irrelevant —
        # even if they would overlap, an unauthenticated request must be rejected.
        user_id = draw(st.sampled_from([None, ""]))
        user_groups = draw(
            st.lists(st.sampled_from(_AUTHORIZED_POOL + _OTHER_POOL), unique=True)
        )
        return {
            "authorized_groups": authorized_groups,
            "user_id": user_id,
            "user_groups": user_groups,
            "category": category,
            "authorized": False,
        }

    # Authenticated: a non-empty user id.
    user_id = draw(st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12))

    if category == "no_overlap":
        # Groups drawn only from the disjoint "other" pool → guaranteed no intersection
        # with authorized_groups (which come from the authorized pool).
        user_groups = draw(st.lists(st.sampled_from(_OTHER_POOL), unique=True))
        return {
            "authorized_groups": authorized_groups,
            "user_id": user_id,
            "user_groups": user_groups,
            "category": category,
            "authorized": False,
        }

    # overlap: include at least one configured authorized group, optionally plus some
    # unrelated groups, in arbitrary order.
    overlap = draw(st.lists(st.sampled_from(authorized_groups), min_size=1, unique=True))
    extras = draw(st.lists(st.sampled_from(_OTHER_POOL), unique=True))
    user_groups = draw(st.permutations(list(set(overlap + extras))))
    return {
        "authorized_groups": authorized_groups,
        "user_id": user_id,
        "user_groups": list(user_groups),
        "category": category,
        "authorized": True,
    }


# Feature: source-control-connector, Property 6: Authorization gate
@settings(max_examples=100)
@given(scenario=_auth_scenarios())
def test_property6_authorization_gate(scenario):
    """A proposal proceeds iff the request-context identity is authenticated AND its
    groups intersect the configured authorized groups; otherwise it is rejected with
    no provider operation (Req 7.1, 7.2, 7.3, 7.4)."""
    config = _make_config(scenario["authorized_groups"])
    fake = FakeProvider()

    # Clear the shared sliding-window store so a prior example's authorized proposal
    # cannot consume this example's rate-limit capacity (the rate-limit gate follows
    # authorization and must not mask its decision).
    security._rate_limit_windows.clear()

    # Identity is derived ONLY from the request contextvar, never from model/tool input.
    request_ctx = {"groups": list(scenario["user_groups"]), "session_id": "s-test"}
    if scenario["user_id"] is not None:
        request_ctx["user_id"] = scenario["user_id"]

    token = set_request_context(request_ctx)
    try:
        with patch("connector.service.get_secret", return_value=_FAKE_CREDENTIAL):
            result = propose_change(
                _INTENT,
                _proposed_files(),
                iac_format="cloudformation",
                title=_TITLE,
                description=_DESCRIPTION,
                config=config,
                provider=fake,
            )
    finally:
        reset_request_context(token)

    if scenario["authorized"]:
        # (c) Authenticated + group overlap: the auth gate passes and, with every later
        # gate satisfied, exactly one proposal is created via the provider.
        assert result.status == "created", (
            f"expected authorized request to create a proposal, got {result.status}: "
            f"{result.message}"
        )
        assert result.pull_request_id is not None
        assert len(fake.calls_for("open_pull_request")) == 1
        assert len(fake.calls_for("create_branch")) == 1
    else:
        # (a)/(b) Unauthenticated OR no group overlap: rejected before any provider op.
        assert result.status == "rejected", (
            f"expected unauthorized request to be rejected, got {result.status}: "
            f"{result.message}"
        )
        assert result.pull_request_id is None
        assert result.pull_request_url is None
        # Req 7.3/7.4: zero source-control operations occurred.
        assert fake.calls == [], f"unauthorized request unexpectedly called provider: {fake.call_operations}"
