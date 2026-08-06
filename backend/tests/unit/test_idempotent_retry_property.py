#!/usr/bin/env python3
"""Property-based test for genuinely idempotent retries / ambiguous-outcome reconciliation.

Covers the v2 idempotency reshape (design Component 6 / Property V6): the Connector derives
a **stable** idempotency key from the request + Verified_Source_Snapshot and a
**deterministic** proposal-branch name from that key, so that when a mutating provider
operation (``create_branch``, ``commit_files``, ``open_change_proposal``) applies its effect
and *then* fails with an ambiguous transient error, reconciling and retrying never creates a
duplicate branch, commit, or Change_Proposal: an existing branch is reused, an already-applied
commit is not repeated, and an already-open Change_Proposal for the deterministic head->base is
returned rather than duplicated (Req 8.1, 8.2).

The test drives ``connector.service.propose_change`` through its full fail-closed pipeline with
an otherwise-authorized request (intersecting groups from the trusted request contextvar,
structurally valid CloudFormation, under the rate limit, and ``base_revision`` equal to the
seeded target-branch head so the read-before-write snapshot verifies). It crosses:

- ``failing_op`` in {``create_branch``, ``commit_files``, ``open_change_proposal``} — the
  mutating op programmed via ``FakeProvider.apply_then_fail`` to land its effect and then raise
  a ``ProviderTransientError`` (the exact "effect landed, then a transient error surfaced"
  ambiguity), and
- ``pre_state`` in {none, branch already exists, proposal already open} — pre-existing
  source-control state seeded before the call, computed from the service's own deterministic
  branch name so the seed lands on exactly the branch the connector will target.

For every combination it asserts:

1. The proposal-branch name is deterministic and stable — a pure function of the request that
   the connector actually used, of shape ``gbaw/<slug>-<12 hex>`` and distinct from the target
   branch (also proven twice against fresh providers below).
2. No duplicate state — at most one created branch for the deterministic name (with no repeated
   branch names), at most one commit for the content on that branch, and exactly one open
   Change_Proposal for the deterministic head->base.
3. Effect-landed-then-raised is recognized by reconcile and not repeated — the applied branch is
   not created twice and an already-open proposal is returned rather than re-opened.
4. The final result is a success (``status == "created"``) carrying the reconciled proposal
   id/url (no false failure), because ``apply_then_fail`` always lands the effect and reconcile
   detects it.

The connector routes audit through the confirming in-memory sink installed by the autouse
``confirmed_audit_sink`` conftest fixture, so no AWS call occurs. ``connector.service.time.sleep``
is patched to a no-op so retry backoff never actually waits. The shared per-user rate-limit
window store is cleared and a unique ``user_id`` is used per example so the rate-limit gate
never rejects a request under test.

Validates: Requirements 8.1, 8.2
"""

# Standard library
import itertools
import json
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector import service
from connector.config import AllowlistEntry, SourceControlConfig
from connector.models import ProposedFile
from connector.provider import ProviderTransientError
from connector.service import (
    _deterministic_branch_name,
    _idempotency_key,
    propose_change,
)
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context
from utils.security import _rate_limit_windows

pytestmark = pytest.mark.unit


# --- Test constants --------------------------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"
_IAC_FORMAT = "cloudformation"

# The mutating provider operations whose ambiguous "effect landed then raised" outcome the
# deterministic-key idempotency must reconcile without ever duplicating state (Property V6).
_MUTATING_OPS = (
    "create_branch",
    "commit_files",
    "open_change_proposal",
)

# Pre-existing source-control state to seed before the call, exercising reconcile-before-run.
_PRE_STATES = ("none", "branch", "proposal")

# Benign, injection-free words so the input-validation / prompt-injection gates always pass and
# every request reaches the provider stage.
_SAFE_WORDS = [
    "update",
    "storage",
    "bucket",
    "queue",
    "configuration",
    "resource",
    "infrastructure",
    "template",
    "service",
    "stack",
]

# CloudFormation resource types used to build structurally valid templates.
_RESOURCE_TYPES = [
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::SNS::Topic",
    "AWS::DynamoDB::Table",
    "AWS::Logs::LogGroup",
]

# A structurally valid CloudFormation template used by the explicit example test.
_VALID_CFN = '{"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}'

# Monotonic source of unique user ids so each example starts with a fresh rate-limit budget and
# a distinct idempotency key.
_user_ids = itertools.count(1)


def _make_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig whose allowlist matches the requested repo/branch."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=(_GROUP,),
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


@st.composite
def _valid_cfn_files(draw):
    """Generate 1..N distinct, structurally valid CloudFormation ``ProposedFile``s."""
    specs = draw(
        st.lists(
            st.tuples(
                st.from_regex(r"[A-Za-z][A-Za-z0-9]{2,15}", fullmatch=True),
                st.sampled_from(_RESOURCE_TYPES),
            ),
            min_size=1,
            max_size=4,
        )
    )
    files: list[ProposedFile] = []
    for index, (logical_id, resource_type) in enumerate(specs):
        template = {"Resources": {logical_id: {"Type": resource_type}}}
        files.append(
            ProposedFile(
                path=f"templates/resource_{index}.yaml",
                content=json.dumps(template),
                iac_format=_IAC_FORMAT,
            )
        )
    return files


def _expected_branch_name(files, intent, user_id, base_revision) -> str:
    """Compute the deterministic proposal-branch name the connector will target.

    Mirrors the service's own derivation (stable idempotency key -> deterministic branch),
    so a test can seed pre-existing state on exactly the branch the connector reconciles
    against. The effective repo/branch are the matched allowlist entry (``_REPO``/``_BRANCH``),
    and the verified base revision equals the seeded target head.
    """
    key = _idempotency_key(
        repo=_REPO,
        target_branch=_BRANCH,
        base_revision=base_revision,
        files=list(files),
        user_id=user_id,
    )
    return _deterministic_branch_name(intent, key)


def _call_propose(fake: FakeProvider, *, intent, files, title, description, user_id, base_revision):
    """Invoke ``propose_change`` for an authorized user with retry backoff neutralized."""
    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with mock.patch("connector.service.time.sleep", return_value=None):
            return propose_change(
                intent,
                files,
                _IAC_FORMAT,
                title,
                description,
                base_revision=base_revision,
                config=_make_config(),
                provider=fake,
            )
    finally:
        reset_request_context(token)


# --- Property V6 -----------------------------------------------------------


# Feature: source-control-connector-v2, Property V6: ambiguous outcomes never produce duplicate source-control state
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    failing_op=st.sampled_from(_MUTATING_OPS),
    pre_state=st.sampled_from(_PRE_STATES),
    base_revision=st.from_regex(r"[0-9a-f]{7,40}", fullmatch=True),
)
def test_propertyV6_ambiguous_outcomes_never_duplicate_state(files, intent_words, failing_op, pre_state, base_revision):
    """A mutating op whose effect landed then raised a transient error is reconciled, not
    repeated: the deterministic branch is reused, the commit is not duplicated, and an
    already-open proposal is returned — never a second branch/commit/proposal (Req 8.1, 8.2)."""
    # Isolate this example: fresh rate-limit budget and a unique user id (also feeds the key).
    _rate_limit_windows.clear()
    user_id = f"user-{next(_user_ids)}"

    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    # The connector's deterministic target branch for this exact logical proposal.
    proposal_branch = _expected_branch_name(files, intent, user_id, base_revision)

    fake = FakeProvider()
    # The target branch head equals the caller's Verified_Source_Snapshot, so the
    # read-before-write check verifies and the provider stage is reached.
    fake.set_head(_REPO, _BRANCH, base_revision)

    # Seed the pre-existing source-control state (reconcile-before-run coverage).
    if pre_state == "branch":
        # The deterministic proposal branch already exists at the base revision (a prior
        # attempt created it but did not commit): create_branch must reuse it, not duplicate.
        fake.set_head(_REPO, proposal_branch, base_revision)
    elif pre_state == "proposal":
        # A full prior attempt already landed: branch advanced past base AND an open proposal
        # exists for the deterministic head->base. Everything must be reused, not duplicated.
        fake.set_head(_REPO, proposal_branch, base_revision)
        fake.advance_head(_REPO, proposal_branch)
        fake.pull_requests.append(
            {
                "repo": _REPO,
                "head": proposal_branch,
                "base": _BRANCH,
                "title": "seeded",
                "body": "seeded",
                "proposal_id": "seeded-1",
                "proposal_url": "https://fake.provider/org/iac-repo/pull/seeded-1",
            }
        )

    # Model the ambiguous outcome: the chosen mutating op applies its effect, THEN raises a
    # transient error. Reconcile-before-retry must observe the applied state and not repeat it.
    fake.apply_then_fail(failing_op, ProviderTransientError("ambiguous transient failure"), times=1)

    result = _call_propose(
        fake,
        intent=intent,
        files=files,
        title=title,
        description=description,
        user_id=user_id,
        base_revision=base_revision,
    )

    # --- 1. Deterministic / stable branch name ------------------------------------------
    assert proposal_branch.startswith("gbaw/")
    assert proposal_branch != _BRANCH
    suffix = proposal_branch.rsplit("-", 1)[-1]
    assert len(suffix) == 12
    assert all(c in "0123456789abcdef" for c in suffix)

    # --- 4. No false failure: the effect landed, so the outcome is a reconciled success --
    assert result.status == "created", result.message
    assert result.proposal_id is not None
    assert result.proposal_url is not None

    # --- 2/3. No duplicate source-control state -----------------------------------------
    # At most one branch is ever *created* for the deterministic name (an existing branch is
    # reused, never re-created), and no branch name is created twice.
    created_for_name = [b for b in fake.created_branches if b["new_branch"] == proposal_branch]
    assert len(created_for_name) <= 1
    all_created = [b["new_branch"] for b in fake.created_branches]
    assert len(all_created) == len(set(all_created))

    # At most one commit lands on the deterministic content-addressed branch.
    commits_for_branch = [c for c in fake.commits if c["branch"] == proposal_branch]
    assert len(commits_for_branch) <= 1

    # Exactly one open Change_Proposal exists for the deterministic head->base — the applied
    # or pre-existing proposal is returned, never duplicated.
    prs_for_head = [pr for pr in fake.pull_requests if pr["head"] == proposal_branch and pr["base"] == _BRANCH]
    assert len(prs_for_head) == 1

    # The connector targeted its deterministic branch (not a random name).
    assert result.proposal_id == prs_for_head[0]["proposal_id"]

    # Determinism across fresh providers: the SAME logical request maps to the SAME branch,
    # so a retry can never fork a second branch (Req 8.1, 8.2).
    fake2 = FakeProvider()
    fake2.set_head(_REPO, _BRANCH, base_revision)
    result2 = _call_propose(
        fake2,
        intent=intent,
        files=files,
        title=title,
        description=description,
        user_id=user_id,
        base_revision=base_revision,
    )
    assert result2.status == "created", result2.message
    rerun_create = fake2.calls_for("create_branch")
    assert rerun_create, "the second run must create the deterministic branch"
    assert rerun_create[0]["new_branch"] == proposal_branch


# --- Explicit example: same request twice against an existing open proposal ------------


def test_v6_same_request_twice_returns_existing_proposal_no_duplicate():
    """A second identical propose against a provider that already has the open proposal returns
    the EXISTING proposal id and opens no duplicate branch/commit/proposal (Req 8.1, 8.2)."""
    _rate_limit_windows.clear()
    user_id = f"user-{next(_user_ids)}"

    intent = "update the storage bucket configuration"
    title = "Update bucket configuration"
    description = "Enable versioning on the storage bucket."
    files = [ProposedFile(path="templates/vpc.yaml", content=_VALID_CFN, iac_format=_IAC_FORMAT)]
    base_revision = "abc123def4567890"

    fake = FakeProvider()
    fake.set_head(_REPO, _BRANCH, base_revision)

    # First run: creates the branch, commit, and exactly one open Change_Proposal.
    first = _call_propose(
        fake,
        intent=intent,
        files=files,
        title=title,
        description=description,
        user_id=user_id,
        base_revision=base_revision,
    )
    assert first.status == "created", first.message
    assert first.proposal_id is not None
    assert len(fake.pull_requests) == 1
    assert len(fake.created_branches) == 1

    # Second identical request against the SAME provider: the deterministic branch already
    # exists, the content-addressed commit already landed, and the proposal is already open —
    # so everything is reconciled and reused, opening no duplicate state.
    second = _call_propose(
        fake,
        intent=intent,
        files=files,
        title=title,
        description=description,
        user_id=user_id,
        base_revision=base_revision,
    )
    assert second.status == "created", second.message
    assert second.proposal_id == first.proposal_id
    assert second.proposal_url == first.proposal_url

    # No duplicate branch, commit, or proposal was created by the second run.
    assert len(fake.pull_requests) == 1
    assert len(fake.created_branches) == 1
    assert len(fake.commits) == 1
