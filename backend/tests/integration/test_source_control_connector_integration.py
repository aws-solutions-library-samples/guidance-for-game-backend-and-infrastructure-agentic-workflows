"""Credential-gated integration round trip for the Source Control Connector.

This test exercises the full read → propose → verify-unmerged → cleanup cycle against a
**real, disposable** GitHub repository. It is an ``@pytest.mark.integration`` test that
**skips cleanly** unless real credentials and a disposable repo are supplied, so it never
runs (or fails) in CI or on a developer machine without the required configuration.

Provide the following environment variables to enable the test:

- ``GBAW_SCM_IT_REPO``   (required) — a disposable repository, e.g. ``"my-org/scm-it-sandbox"``.
- ``GBAW_SCM_IT_TOKEN``  (required) — a GitHub token with ``contents:write`` and
  ``pull_requests:write`` on that repo (test-only; a fine-grained PAT scoped to the sandbox
  is recommended).
- ``GBAW_SCM_IT_BRANCH`` (optional) — the target branch, default ``"main"``.
- ``GBAW_SCM_IT_FILE``   (optional) — an existing repository-relative IaC file path to read,
  default ``"README.md"``.

When these are absent the test skips with a clear message.

What it validates (Req 2.1, 2.2, 2.6, 6.1):

1. Reads an existing file from the disposable repo through the connector service
   (``read_iac_files``) — Req 3.1 read path used to satisfy the "read a file" step.
2. Opens a **real** pull request through the connector's propose pipeline
   (``propose_change``) — Req 2.1 (unique branch off the target head), Req 2.2 (exactly one
   Change_Proposal), Req 2.6 (returns id + URL).
3. Confirms the PR exists and is **unmerged** — Req 6.1.
4. Cleans up **out of band** (directly via the GitHub REST API, NOT through the connector,
   which deliberately has no close/merge operation): closes the PR and deletes the proposal
   branch, so the test is idempotent and leaves no residue.

Under the v2 ProviderAuth model, credential acquisition is owned entirely by the
Provider_Adapter: the GitHub adapter's ``GitHubTokenAuth`` fetches the SCM_Credential via
``get_secret(..., source="secretsmanager")``. To let this integration test run with just a
GitHub token (no Secrets Manager provisioning), ``get_secret`` is patched inside the adapter
module (``connector.github_provider``) to return the test token. The connector core no longer
calls ``get_secret`` at all. Every other connector code path — enablement gate,
authorization, allowlist, rate limit, IaC validation, provider ops — runs unmodified.
"""

# Standard library
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

# Third-party packages
import httpx
import pytest

# Add src to path so connector modules import under the pytest.ini `pythonpath = src tests`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# Local modules
from connector.config import AllowlistEntry, SourceControlConfig
from connector.service import propose_change, read_iac_files
from support.config_factory import make_source_control_config
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.integration

# --- Credential / disposable-repo gating -----------------------------------------------
_REPO = os.getenv("GBAW_SCM_IT_REPO")
_TOKEN = os.getenv("GBAW_SCM_IT_TOKEN")
_BRANCH = os.getenv("GBAW_SCM_IT_BRANCH", "main")
_READ_FILE = os.getenv("GBAW_SCM_IT_FILE", "README.md")

_CREDENTIALS_PRESENT = bool(_REPO and _TOKEN)
_SKIP_REASON = (
    "Source Control Connector integration test skipped: set GBAW_SCM_IT_REPO and "
    "GBAW_SCM_IT_TOKEN (and optionally GBAW_SCM_IT_BRANCH / GBAW_SCM_IT_FILE) to run it "
    "against a disposable GitHub repository."
)

_AUTHORIZED_GROUP = "scm-proposers"
_GITHUB_API = "https://api.github.com"


def _build_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig scoped to the disposable repo/branch.

    Built via the split-contract factory so the allowlist/authorized groups land on the
    domain contract, the operational tuning + audit destination on the neutral connector
    contract, and the credential + base URL on the adapter contract.
    """
    return make_source_control_config(
        enabled=True,
        provider="github",
        # Sentinel id: get_secret is patched to return the real token, so the connector
        # never needs a provisioned Secrets Manager secret for this test.
        credential_secret_id="scm-it-token",
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=(_AUTHORIZED_GROUP,),
        rate_limit_max=5,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _gh_headers() -> dict:
    """Direct GitHub API headers for out-of-band cleanup/verification (not via connector)."""
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_pull_request(pr_number: str) -> dict:
    """Fetch a PR directly from GitHub to verify state and discover its head branch."""
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_GITHUB_API}/repos/{_REPO}/pulls/{pr_number}", headers=_gh_headers()
        )
        response.raise_for_status()
        return response.json()


def _cleanup_pull_request(pr_number: str | None, head_branch: str | None) -> None:
    """Close the PR and delete its branch out of band so the test is idempotent.

    This uses the GitHub REST API directly, NOT the connector, because the connector
    deliberately exposes no close/merge/delete operation (Req 2.5, 6.1/6.2). Failures here
    are swallowed so cleanup never masks the real assertion outcome.
    """
    with httpx.Client(timeout=30.0) as client:
        if pr_number:
            try:
                client.patch(
                    f"{_GITHUB_API}/repos/{_REPO}/pulls/{pr_number}",
                    headers=_gh_headers(),
                    json={"state": "closed"},
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        if head_branch:
            try:
                client.delete(
                    f"{_GITHUB_API}/repos/{_REPO}/git/refs/heads/{head_branch}",
                    headers=_gh_headers(),
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


_MINIMAL_CLOUDFORMATION = """AWSTemplateFormatVersion: '2010-09-09'
Description: Disposable resource proposed by the GBAW connector integration test.
Resources:
  ScmConnectorItParameter:
    Type: AWS::SSM::Parameter
    Properties:
      Type: String
      Value: gbaw-scm-connector-integration-test
"""


@pytest.mark.skipif(not _CREDENTIALS_PRESENT, reason=_SKIP_REASON)
def test_connector_round_trip_reads_file_and_opens_unmerged_pr():
    """Read a file, open a real PR, confirm it is unmerged, then close it out of band."""
    config = _build_config()

    # Unique proposed file path keeps every run independent and easy to clean up.
    unique = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    proposed_path = f"gbaw-scm-it/{unique}.yaml"

    pr_number: str | None = None
    head_branch: str | None = None

    # Patch the credential fetch in the adapter module (the only connector module that calls
    # get_secret now that credential acquisition is adapter-owned behind ProviderAuth) so the
    # real GitHub token is used for provider auth, while every other connector code path runs
    # unmodified.
    with patch("connector.github_provider.get_secret", return_value=_TOKEN):
        # Identity must come from the request context, never from tool/model input (Req 7.1).
        token = set_request_context(
            {
                "user_id": "scm-integration-test",
                "groups": [_AUTHORIZED_GROUP],
                "session_id": f"scm-it-{unique}",
            }
        )
        try:
            # --- Step 1: read an existing file from the disposable repo (read path) --------
            read_result = read_iac_files([_READ_FILE], config=config)
            assert read_result.limit_exceeded is False
            # The configured read file should exist in a properly prepared disposable repo.
            assert _READ_FILE not in read_result.missing, (
                f"Expected '{_READ_FILE}' to exist in {_REPO}@{_BRANCH}; "
                f"set GBAW_SCM_IT_FILE to an existing path."
            )
            assert len(read_result.files) == 1
            assert read_result.files[0].path == _READ_FILE
            # The read captured the Verified_Source_Snapshot (the target-branch head); it is
            # the opaque revision the propose step must be anchored to (read-before-write).
            assert read_result.revision, "expected the read to capture a source revision"
            base_revision = read_result.revision

            # --- Step 2: open a REAL pull request via the propose pipeline -----------------
            from connector.models import ProposedFile

            proposal = propose_change(
                intent="Add a disposable SSM parameter for connector integration testing",
                files=[
                    ProposedFile(
                        path=proposed_path,
                        content=_MINIMAL_CLOUDFORMATION,
                        iac_format="cloudformation",
                    )
                ],
                iac_format="cloudformation",
                title="[GBAW IT] Disposable connector integration-test proposal",
                description=(
                    "Automated integration-test proposal. Safe to close. Adds a single "
                    "disposable SSM parameter template."
                ),
                base_revision=base_revision,
                config=config,
            )

            assert proposal.status == "created", f"Unexpected proposal result: {proposal}"
            assert proposal.proposal_id, "Expected a pull request id (Req 2.6)"
            assert proposal.proposal_url, "Expected a pull request URL (Req 2.6)"
            pr_number = proposal.proposal_id

            # --- Step 3: confirm the PR exists and is UNMERGED (Req 6.1) -------------------
            pr = _get_pull_request(pr_number)
            head_branch = pr.get("head", {}).get("ref")
            assert pr.get("state") == "open", "A freshly opened proposal must be open"
            assert pr.get("merged") is False, "Change proposal must be unmerged (Req 6.1)"
            assert pr.get("base", {}).get("ref") == _BRANCH
            # The connector generates a gbaw/-prefixed proposal branch off the target head.
            assert head_branch and head_branch.startswith("gbaw/"), (
                f"Expected a gbaw/ proposal branch, got {head_branch!r}"
            )
        finally:
            # --- Step 4: clean up out of band so the test is idempotent --------------------
            _cleanup_pull_request(pr_number, head_branch)
            reset_request_context(token)
