"""Documentation contract tests for docs/THREAT_MODEL.md (issue #280).

These tests are deliberately *documentation* guards, not runtime behavior
tests. They fail the build when the published threat model drifts away from the
boundaries fixed by the accepted ADRs and the versioned Operations Contracts, so
a stale or dangerously optimistic trust-boundary claim cannot land silently.

They assert three kinds of invariant:

1. Structural: the document keeps the deployed read-only chat path and the
   optional operations control plane as separate, clearly labelled parts.
2. Semantic: the mandatory "these are never authorization" and "writes are
   human-approved / default-disabled" statements remain present.
3. Referential: every intra-repo Markdown link and same-document anchor the
   threat model uses actually resolves, so renaming a section elsewhere cannot
   leave a dangling trust-boundary reference.
"""

from __future__ import annotations

# Standard library
import re
from pathlib import Path

# Third-party packages
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS = _REPO_ROOT / "docs"
_THREAT_MODEL = _DOCS / "THREAT_MODEL.md"


def _strip_markdown_emphasis(value: str) -> str:
    """Drop ``*`` emphasis markers so phrase checks ignore bold/italic styling."""
    return value.replace("*", "")


@pytest.fixture(scope="module")
def text() -> str:
    return _THREAT_MODEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def plain(text: str) -> str:
    """Lowercased text with emphasis markers removed for phrase assertions."""
    return _strip_markdown_emphasis(text).lower()


def _slugify(heading: str) -> str:
    """GitHub-flavored anchor slug for a Markdown heading."""
    slug = heading.strip().lower()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^a-z0-9\-_]", "", slug)
    return slug


@pytest.mark.unit
@pytest.mark.fast
def test_threat_model_exists(text: str) -> None:
    assert text.strip(), "THREAT_MODEL.md must not be empty"


@pytest.mark.unit
@pytest.mark.fast
def test_keeps_deployed_and_planned_scopes_separate(text: str) -> None:
    """The deployed chat path and the planned operations plane stay distinct."""
    assert "# Part I: Deployed Read-Only Chat Path" in text
    assert "# Part II: Optional Operations Control Plane (Planned)" in text
    # The read-only deployed boundary must be preserved, not replaced.
    assert "read-only" in text.lower()
    assert re.search(
        r"read[- ]only iam.*not", text.lower()
    ), "Must state that read-only IAM is not future write authorization"


@pytest.mark.unit
@pytest.mark.fast
def test_core_untrusted_invariants_present(plain: str) -> None:
    """Model output, operation ids, and automation identities are never authz."""
    assert "operation identifier is neither a credential nor authorization" in plain
    assert "untrusted" in plain and "proposal" in plain
    # Model output must be explicitly denied authority.
    assert re.search(
        r"model.*(never|cannot).*(authoriz|grant)", plain
    ), "Model output must be explicitly stated as non-authoritative"


@pytest.mark.unit
@pytest.mark.fast
def test_distinct_identities_enumerated(plain: str) -> None:
    for identity in ("requester", "approver", "automation", "workflow", "executor", "chat-runtime"):
        assert identity in plain, f"Identity '{identity}' must be named as distinct"


@pytest.mark.unit
@pytest.mark.fast
def test_operations_default_disabled_and_writes_human_approved(plain: str) -> None:
    assert "default-disabled" in plain
    assert "gbaw_operations_mode" in plain
    assert "creates no operations resources" in plain
    assert re.search(
        r"source-control (provider )?writes always require human approval", plain
    ), "Source-control writes must be stated as always human-approved"
    # Repository review is only an additional gate when repository policy enforces it.
    assert re.search(
        r"only when repository policy enforces it", plain
    ), "Repository review must be conditional on enforced repository policy"


@pytest.mark.unit
@pytest.mark.fast
def test_separate_write_and_executor_roles(plain: str) -> None:
    assert "read and write credentials are separate" in plain
    assert "separate prepared executor" in plain or "narrow per-capability" in plain
    # Chat runtime stays provider-read-only.
    assert re.search(r"chat.*(role|runtime).*read-only", plain)


@pytest.mark.unit
@pytest.mark.fast
def test_required_operations_boundaries_and_threats_present(text: str) -> None:
    """Every planned trust boundary O1-O8 and the key threat IDs appear."""
    for boundary in ("O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8"):
        assert re.search(rf"\b{boundary}\b", text), f"Boundary {boundary} missing"
    # Spot-check threat coverage the issue enumerates explicitly.
    for threat_id in (
        "OP-X1",  # direct executor invocation
        "OP-X2",  # operation-identifier guessing
        "OP-A2",  # approval binds to prepared content
        "OP-A3",  # self-approval
        "OP-A4",  # replay / stale approval
        "OP-C1",  # unauthorized cancellation
        "OP-SC1",  # read/write credential separation
        "OP-SC2",  # path traversal
        "OP-SC6",  # CI/bots/preview/auto-merge
        "OP-AU3",  # loops / budget / blast radius
        "OP-AU5",  # emergency disablement
        "OP-L1",  # append-only ledger
        "OP-L7",  # version drift / tampering
        "OP-CD2",  # indirect prompt injection / confused deputy
    ):
        assert threat_id in text, f"Threat {threat_id} must be covered"


@pytest.mark.unit
@pytest.mark.fast
def test_residual_risks_and_live_exercises_explicit(plain: str) -> None:
    assert "residual" in plain
    assert "live exercise" in plain
    assert "fail closed" in plain or "fail-closed" in plain


@pytest.mark.unit
@pytest.mark.fast
def test_every_status_leaf_labelled(text: str) -> None:
    """Attack-tree leaves must carry an explicit status marker, never bare."""
    leaf_lines = [line for line in text.splitlines() if "└──" in line or "├──" in line]
    resolved = [ln for ln in leaf_lines if "[" in ln and "]" in ln]
    # At least the resolving leaves must be status-tagged; ensure we actually
    # found some (guards against the trees being deleted).
    assert resolved, "Attack trees with status-tagged leaves must be present"
    for ln in resolved:
        assert re.search(
            r"\[(PREVENTED|PLANNED|MITIGATED|PARTIAL|RESIDUAL)", ln
        ), f"Attack-tree leaf missing a recognized status tag: {ln.strip()}"


@pytest.mark.unit
@pytest.mark.fast
def test_intra_repo_links_and_anchors_resolve(text: str) -> None:
    """Every relative Markdown link the threat model uses must resolve.

    This is what makes a stale trust-boundary reference fail the build: if an
    ADR/contract section is renamed or removed, the anchor no longer resolves
    and this test breaks.
    """
    self_anchors = {_slugify(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", text, flags=re.MULTILINE)}

    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    checked = 0
    for match in link_pattern.finditer(text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue  # external links are covered by other checks
        path_part, _, anchor = target.partition("#")
        if path_part == "":
            # Same-document anchor.
            assert anchor in self_anchors, f"Dangling same-doc anchor: #{anchor}"
            checked += 1
            continue
        # Relative file link, resolved from the docs directory.
        resolved = (_DOCS / path_part).resolve()
        assert resolved.exists(), f"Broken relative link target: {target}"
        checked += 1
        if anchor:
            body = resolved.read_text(encoding="utf-8")
            anchors = {_slugify(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", body, flags=re.MULTILINE)}
            assert anchor in anchors, f"Broken anchor in {path_part}: #{anchor}"
    assert checked > 0, "Expected at least one intra-repo link to validate"


@pytest.mark.unit
@pytest.mark.fast
def test_version_bumped(text: str) -> None:
    """The published version must reflect the operations expansion."""
    assert re.search(
        r"\*\*Version\*\*:\s*2\.\d+", text
    ), "THREAT_MODEL.md version must be >= 2.0 after the operations expansion"
    assert "issue #280" in text
