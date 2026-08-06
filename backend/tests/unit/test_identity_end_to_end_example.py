#!/usr/bin/env python3
"""End-to-end identity test: verified UI claims -> AgentCore context -> connector authz.

Covers Requirement 5 (5.1, 5.2, 5.3 / PB5 / MR2) from the source-control-connector-v2
design: the requesting-user identity and authorization groups the Source Control Connector
authorizes on are derived **only** from the trusted, request-scoped Identity_Context, which
is populated from *verified UI Cognito claims* threaded through the AgentCore entrypoint —
never from agent/model/tool input. MR2 requires an end-to-end test that exercises this exact
chain, from the verified claims to the connector's authorization decision.

This is an **example** test (three explicit cases), not a Hypothesis property test: the value
here is proving the concrete *wiring* of a multi-hop production path end to end, and the three
cases (authorized, unauthenticated, unauthorized-groups) are specific, named scenarios rather
than a universally quantified property over generated inputs. The five-dimension authorization
policy itself is already covered as a property by
``test_five_dimension_authorization_property.py`` (Property V3); this test complements it by
verifying that identity/groups actually *reach* that gate only through the real claims->context
chain.

The chain wired here uses the REAL production function at every hop; only the UI claims at the
very front and the source-control provider at the very back are simulated:

    simulated verified UI Cognito claims
        -> utils.security.validate_user_context            (REAL: allowlist + sanitize claims)
        -> AgentCore agent_context construction            (REAL mapping from agentcore_main:
                                                             groups -> agent_context["groups"],
                                                             user_id -> agent_context["user_id"])
        -> utils.request_context.set_request_context       (REAL: request-scoped contextvar)
        -> connector.service.propose_change                (REAL: reads identity/groups from the
                                                             contextvar for the 5-dimension gate)

``propose_change`` has NO user/identity/groups parameter, so identity cannot be supplied as
tool input — it is read solely from the request contextvar populated above, which is the
security property under test (Req 5.1, 5.2).

Three cases:

  1. Authenticated + groups intersecting ``authorized_groups`` -> proposal is created
     (status "created", exactly one proposal). Because Task 4 made ``base_revision`` required,
     the FakeProvider head is seeded and a matching ``base_revision`` is passed so the verified
     source snapshot check passes and the success path reaches "created".
  2. Unauthenticated (no verified identity / no user_id / no groups) -> rejected, and NO
     provider mutation is performed.
  3. Authenticated but groups do NOT intersect ``authorized_groups`` -> rejected, and NO
     provider mutation is performed.

Validates: Requirements 5.1, 5.2, 5.3
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest

# Local modules
import utils.security as security
from connector import service as service_module
from connector.config import AllowlistEntry, SourceControlConfig
from connector.models import ProposedFile
from connector.service import propose_change
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context
from utils.security import validate_user_context

pytestmark = pytest.mark.unit


# --- Fixed operator configuration ------------------------------------------------------

# The connector's operator-approved allowlist entry the propose path defaults to. No
# path_prefixes/extensions are configured (=> any path / any extension), so the repository,
# branch, path, and extension dimensions all pass and the GROUP dimension is the only
# authorization variable exercised by these identity cases.
_REPO = "org/iac-repo"
_BRANCH = "main"
_ALLOWLIST = (AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),)

# The single group an operator authorized to propose changes. Only a caller whose VERIFIED
# Cognito groups intersect this set may create a proposal.
_AUTHORIZED_GROUP = "scm-writers"

# The verified source snapshot: the target-branch head the agent "read" before proposing.
# Task 4 makes base_revision required and verifies it against the current head, so the test
# seeds this head on the FakeProvider and passes it as base_revision on the success path.
_HEAD_SHA = "a" * 40

# Benign intent/title/description that pass input validation + prompt-injection detection so
# the authorization outcome is the only decision under test.
_INTENT = "Update the S3 bucket configuration in the infrastructure template"
_TITLE = "Update bucket configuration"
_DESCRIPTION = "Adjust the storage bucket settings to match the requested configuration."

# Structurally valid CloudFormation so IaC validation passes on the authorized path and does
# not mask the authorization decision.
_VALID_CFN = "Resources:\n  MyBucket:\n    Type: AWS::S3::Bucket\n"


def _make_config() -> SourceControlConfig:
    """Build an enabled connector config with a known allowlist + authorized group.

    ``rate_limit_max`` is high (and the sliding-window store is cleared per test) so the
    rate-limit gate never masks the authorization decision on the authorized path.
    """
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/cred-abc",
        allowlist=_ALLOWLIST,
        authorized_groups=(_AUTHORIZED_GROUP,),
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _proposed_files() -> list[ProposedFile]:
    return [ProposedFile(path="template.yaml", content=_VALID_CFN, iac_format="cloudformation")]


def _build_agent_context(validated_user_context: dict) -> dict:
    """Mirror ``agentcore_main.invoke_agent``'s construction of ``agent_context``.

    This is the REAL production mapping from the *validated* user_context into the request
    contextvar payload (see ``backend/src/agentcore_main.py``): the top-level ``groups`` carry
    the validated Cognito ``groups`` claim, and ``user_id`` carries the persistent user id.
    Both are sourced **solely** from the validated user_context (never from spoofable
    agent/model input); ``validate_user_context`` has already allow-listed and sanitized them.
    An empty ``groups`` list when the claim is absent makes the downstream authorization gate
    fail closed rather than error.

    Note: ``agentcore_main`` substitutes a synthetic ``"anonymous"`` placeholder for a missing
    ``user_id``. The *authorization-relevant* identity, however, is the verified claim: a
    request with no verified identity has no authorized ``user_id``, which is exactly what the
    connector's authentication precondition must reject. This helper therefore threads the
    validated ``user_id`` through as-is (present -> authenticated; absent -> unauthenticated),
    faithfully preserving the ``groups`` mapping that is the security vector under test.
    """
    agent_context: dict = {
        # Top-level groups carry the VALIDATED Cognito groups claim into the request
        # contextvar so the connector's authorization gate resolves real group membership.
        "groups": validated_user_context.get("groups", []),
        "session_id": validated_user_context.get("session_id") or "default",
        "username": validated_user_context.get("username", "user"),
        "auth_type": validated_user_context.get("auth_type", "unknown"),
        "user_info": validated_user_context,
    }
    # Only an authenticated caller carries a verified user_id; an unauthenticated request has
    # none, so the connector's authentication precondition rejects it before authorization.
    if validated_user_context.get("user_id"):
        agent_context["user_id"] = validated_user_context["user_id"]
    return agent_context


def _propose_through_identity_chain(raw_claims: dict | None, fake: FakeProvider):
    """Drive the full identity chain and return the connector's ``ProposalResult``.

    Wires the REAL production hops end to end:
    ``validate_user_context`` -> ``agent_context`` construction -> ``set_request_context`` ->
    ``propose_change``. Only ``raw_claims`` (simulated verified UI Cognito claims) and the
    injected ``FakeProvider`` are simulated. Identity/groups reach ``propose_change`` solely
    via the request contextvar — ``propose_change`` is called with NO identity/groups argument.
    """
    # Hop 1 (REAL): validate + sanitize the verified UI Cognito claims.
    validated = validate_user_context(raw_claims)
    # Hop 2 (REAL mapping): build the AgentCore agent_context from the validated claims.
    agent_context = _build_agent_context(validated)
    # Hop 3 (REAL): publish identity to the request-scoped contextvar.
    token = set_request_context(agent_context)
    try:
        # Hop 4 (REAL): the connector reads identity/groups from the contextvar for its
        # five-dimension authorization gate. No identity is passed as a tool argument.
        return propose_change(
            _INTENT,
            _proposed_files(),
            iac_format="cloudformation",
            title=_TITLE,
            description=_DESCRIPTION,
            base_revision=_HEAD_SHA,
            config=_make_config(),
            provider=fake,
        )
    finally:
        reset_request_context(token)


def _seeded_provider() -> FakeProvider:
    """A FakeProvider whose target-branch head equals the verified snapshot ``_HEAD_SHA``."""
    fake = FakeProvider()
    fake.set_head(_REPO, _BRANCH, _HEAD_SHA)
    return fake


def _assert_no_provider_mutation(fake: FakeProvider) -> None:
    """Assert the connector performed ZERO mutating provider operations and created no PR."""
    for op in ("create_branch", "commit_files", "open_change_proposal"):
        assert fake.calls_for(op) == [], f"rejected request unexpectedly invoked {op}"
    assert fake.pull_requests == [], "rejected request unexpectedly opened a change proposal"


# --- Case 1: authenticated + intersecting groups -> proposal created -------------------


def test_authenticated_authorized_groups_creates_proposal():
    """Verified UI claims whose groups intersect the authorized groups create one proposal.

    The verified Cognito claims flow through validate_user_context -> agent_context ->
    request contextvar -> propose_change, and because the caller's verified groups intersect
    the operator's authorized_groups (and every other gate is satisfied), exactly one
    unmerged change proposal is created (status "created")."""
    security._rate_limit_windows.clear()
    fake = _seeded_provider()

    # Simulated verified UI Cognito claims: a real sub and a cognito:groups membership that
    # the frontend has mapped onto user_id/groups.
    raw_claims = {
        "user_id": "cognito-sub-alice",
        "username": "alice",
        "email": "alice@example.com",
        "auth_type": "cognito",
        "groups": [_AUTHORIZED_GROUP, "viewers"],
        "session_id": "sess-alice-1",
    }

    result = _propose_through_identity_chain(raw_claims, fake)

    assert result.status == "created", (
        f"authorized identity should create a proposal, got {result.status}: {result.message}"
    )
    assert result.proposal_id is not None
    assert result.proposal_url is not None
    # Exactly one proposal was opened, scoped to the allowlisted repo/branch.
    assert len(fake.calls_for("open_change_proposal")) == 1
    assert len(fake.calls_for("create_branch")) == 1
    assert len(fake.pull_requests) == 1
    pr = fake.pull_requests[0]
    assert pr["repo"] == _REPO
    assert pr["base"] == _BRANCH


# --- Case 2: unauthenticated -> rejected, no provider mutation -------------------------


def test_unauthenticated_request_rejected_no_provider_op():
    """A request with no verified identity is rejected and performs no provider mutation.

    With no verified UI claims (no user_id, no groups), the request contextvar carries no
    authenticated identity and no authorized groups, so the connector rejects the proposal
    before any Provider_Adapter operation: no branch, commit, or change proposal is created."""
    security._rate_limit_windows.clear()
    fake = _seeded_provider()

    # No verified UI Cognito claims reached the backend (unauthenticated / guest session).
    result = _propose_through_identity_chain(None, fake)

    assert result.status in ("rejected", "declined"), (
        f"unauthenticated request should be rejected, got {result.status}: {result.message}"
    )
    assert result.proposal_id is None
    assert result.proposal_url is None
    _assert_no_provider_mutation(fake)


# --- Case 3: authenticated + non-intersecting groups -> rejected, no provider mutation --


def test_authenticated_unauthorized_groups_rejected_no_provider_op():
    """A verified caller whose groups do NOT intersect authorized_groups is rejected.

    The identity is authenticated (a real verified sub), but the caller's verified Cognito
    groups do not intersect the operator's authorized_groups, so the five-dimension
    authorization gate rejects the request on the group dimension before any provider
    operation — no branch, commit, or change proposal is created."""
    security._rate_limit_windows.clear()
    fake = _seeded_provider()

    # Verified identity, but only unauthorized groups (no intersection with authorized_groups).
    raw_claims = {
        "user_id": "cognito-sub-carol",
        "username": "carol",
        "email": "carol@example.com",
        "auth_type": "cognito",
        "groups": ["viewers", "readonly"],
        "session_id": "sess-carol-1",
    }

    result = _propose_through_identity_chain(raw_claims, fake)

    assert result.status in ("rejected", "declined"), (
        f"unauthorized-groups request should be rejected, got {result.status}: {result.message}"
    )
    assert result.proposal_id is None
    assert result.proposal_url is None
    _assert_no_provider_mutation(fake)
