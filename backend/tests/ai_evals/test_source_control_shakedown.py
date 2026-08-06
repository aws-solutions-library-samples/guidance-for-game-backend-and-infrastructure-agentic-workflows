"""
Live_Shakedown smoke — Source Control Connector IaC_Change_Specialist (R12.4 / MR4).

A post-deploy smoke that drives ONE read + ONE propose **through the specialist's
agent-facing tools** (``get_iac_file`` → ``propose_infrastructure_change``) against a
disposable repository and asserts an **unmerged** Change_Proposal is created. It never
merges, approves, or closes anything (the connector deliberately exposes no such
operation), so it is read + propose only.

Marker + skip conventions mirror the other ai_evals modules (``test_ground_truth.py``,
``test_agent_evals.py``): the module is ``cloud`` + ``ai_eval`` marked, so it is
**deselected** under ``-m "not integration and not e2e and not cloud"`` and only runs
against a deployed stack. On top of the deployed-stack gate it adds two connector-specific
skip gates so it can never fail when the feature is off or unconfigured:

1. **Deployed stack required.** Uses the shared ``get_test_config()`` (same detection as
   ``test_ground_truth``); skips unless a deployed stack is detected.
2. **Connector must be ENABLED.** Mirrors how the connector itself decides enablement —
   ``SourceControlConfig.load().enabled`` over the deployed ``GBAW_SCM_*`` environment.
   When the connector is disabled (the default), the test SKIPS — this preserves
   disabled-by-default and must never fail when the feature is off (Req 1.1, 1.2).
3. **Disposable-repo inputs required.** Mirrors the integration test's env-var gating
   (``GBAW_SCM_IT_REPO`` / ``GBAW_SCM_IT_TOKEN``, optional ``GBAW_SCM_IT_BRANCH`` /
   ``GBAW_SCM_IT_FILE``). When these are absent the test SKIPS with a clear reason.

Under the v2 ProviderAuth model credential acquisition is adapter-owned; to let the
in-process specialist tools authenticate against the disposable repo with just a test
token (no Secrets Manager provisioning), ``get_secret`` is patched inside the adapter
module (``connector.github_provider``) to return the test token — exactly as the
credential-gated integration round trip does. Every other connector code path
(enablement gate, five-dimension authorization, allowlist, rate limit, IaC validation,
read-before-write snapshot, audit) runs unmodified.

_Traceability: Live_Shakedown → R12.4, MR4 (design.md → Component 7)._
"""

# Standard library
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

# Third-party packages
import pytest

# Add src to path so connector modules import under the pytest.ini `pythonpath = src tests`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# Local modules
from connector.config import SourceControlConfig
from connector.tools import get_iac_file, propose_infrastructure_change
from utils.request_context import reset_request_context, set_request_context

from .test_config import get_test_config

pytestmark = [pytest.mark.cloud, pytest.mark.ai_eval]

# --- Disposable-repo gating (mirrors the integration round trip) -----------------------
_REPO = os.getenv("GBAW_SCM_IT_REPO")
_TOKEN = os.getenv("GBAW_SCM_IT_TOKEN")
_BRANCH = os.getenv("GBAW_SCM_IT_BRANCH", "main")
_READ_FILE = os.getenv("GBAW_SCM_IT_FILE", "README.md")

_INPUTS_PRESENT = bool(_REPO and _TOKEN)
_INPUTS_SKIP_REASON = (
    "Source Control Live_Shakedown skipped: set GBAW_SCM_IT_REPO and GBAW_SCM_IT_TOKEN "
    "(and optionally GBAW_SCM_IT_BRANCH / GBAW_SCM_IT_FILE) to run it against a disposable "
    "repository."
)

_MINIMAL_CLOUDFORMATION = """AWSTemplateFormatVersion: '2010-09-09'
Description: Disposable resource proposed by the GBAW connector live shakedown.
Resources:
  ScmConnectorShakedownParameter:
    Type: AWS::SSM::Parameter
    Properties:
      Type: String
      Value: gbaw-scm-connector-live-shakedown
"""


@pytest.fixture(scope="module")
def connector_config():
    """Deployed + connector-enabled config gate.

    Skips the whole module unless (1) a deployed stack is detected and (2) the connector
    is ENABLED in that deployed environment — determined exactly as the connector itself
    determines it, via ``SourceControlConfig.load().enabled``. A disabled connector skips
    (never fails), preserving disabled-by-default.
    """
    if get_test_config()["mode"] != "deployed":
        pytest.skip("Live_Shakedown requires a deployed stack")

    config = SourceControlConfig.load()
    if not config.enabled:
        pytest.skip(
            "Source Control Connector is disabled in the deployed stack "
            "(disabled-by-default preserved); Live_Shakedown skipped"
        )
    if not config.domain.authorized_groups:
        pytest.skip("Enabled connector has no authorized groups configured; nothing to exercise")
    return config


@pytest.mark.skipif(not _INPUTS_PRESENT, reason=_INPUTS_SKIP_REASON)
def test_shakedown_read_then_propose_creates_unmerged_proposal(connector_config):
    """Read one IaC file, then propose a change through the specialist tools; assert an
    unmerged Change_Proposal is created and anchored to the read's verified snapshot."""
    # Authorization groups come only from the trusted request-scoped Identity_Context,
    # never from tool/model input (Req 5.1). Use a group the deployed policy authorizes.
    authorized_group = connector_config.domain.authorized_groups[0]
    unique = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    proposed_path = f"gbaw-scm-shakedown/{unique}.yaml"

    # Adapter-owned credential (v2 ProviderAuth): patch the adapter's get_secret so the
    # in-process specialist tools authenticate against the disposable repo with the test
    # token. The connector core issues no get_secret itself.
    with patch("connector.github_provider.get_secret", return_value=_TOKEN):
        token = set_request_context(
            {
                "user_id": "scm-shakedown",
                "groups": [authorized_group],
                "session_id": f"scm-shakedown-{unique}",
            }
        )
        try:
            # --- Step 1: read through the specialist's read tool -----------------------
            read_result = get_iac_file(
                [_READ_FILE], repository=_REPO, target_branch=_BRANCH
            )
            assert "error" not in read_result, f"Read failed: {read_result.get('error')}"
            assert read_result["limit_exceeded"] is False
            assert _READ_FILE not in read_result["missing"], (
                f"Expected '{_READ_FILE}' to exist in {_REPO}@{_BRANCH}; "
                f"set GBAW_SCM_IT_FILE to an existing path."
            )
            # Read-before-write: the read captures the Verified_Source_Snapshot (revision)
            # that the propose step MUST be anchored to (Req 7.1, 7.2).
            base_revision = read_result["revision"]
            assert base_revision, "expected the read to capture a source revision"

            # --- Step 2: propose through the specialist's propose tool -----------------
            proposal = propose_infrastructure_change(
                intent="Add a disposable SSM parameter for connector live shakedown",
                files=[
                    {
                        "path": proposed_path,
                        "content": _MINIMAL_CLOUDFORMATION,
                        "iac_format": "cloudformation",
                    }
                ],
                iac_format="cloudformation",
                title="[GBAW Shakedown] Disposable connector live-shakedown proposal",
                description=(
                    "Automated live-shakedown proposal. Safe to close. Adds a single "
                    "disposable SSM parameter template."
                ),
                base_revision=base_revision,
                repository=_REPO,
                target_branch=_BRANCH,
            )

            # --- Assert an UNMERGED Change_Proposal was created ------------------------
            # The connector only ever creates unmerged proposals (it exposes no
            # merge/approve/close), so a "created" status with an id + url is an unmerged
            # proposal awaiting human review (Req 2.1, 3.4).
            assert proposal["status"] == "created", f"Unexpected proposal result: {proposal}"
            assert proposal["proposal_id"], "Expected a change proposal id"
            assert proposal["proposal_url"], "Expected a change proposal URL"
        finally:
            # Read + propose only — deliberately no merge/approve/close from this smoke.
            reset_request_context(token)
