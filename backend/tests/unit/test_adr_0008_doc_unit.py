"""Documentation contract tests for ADR 0008 (issue #555).

These are *documentation* guards, not runtime behavior tests. They fail the
build when the proposed handoff ADR drifts from two invariants a reader of
``main`` depends on:

1. Referential: every intra-repo relative file link the ADR uses must resolve,
   so the record cannot cite a path that does not exist in this checkout.
2. Grounding: the ADR describes work that is not yet merged. Surfaces that do
   not exist on ``main`` (the proposal proxy, the capacity prepare contract,
   the discovery reuse, token-audience separation) must be written as proposed
   or issue-tracked, never asserted as already-built, present-tense code. The
   record is ``Proposed``; it must say so.
"""

from __future__ import annotations

# Standard library
import re
from pathlib import Path

# Third-party packages
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADR_DIR = _REPO_ROOT / "docs" / "adr"
_ADR = _ADR_DIR / "0008-hand-off-chat-proposals-to-direct-approval.md"


@pytest.fixture(scope="module")
def text() -> str:
    return _ADR.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.fast
def test_adr_exists(text: str) -> None:
    assert text.strip(), "ADR 0008 must not be empty"


@pytest.mark.unit
@pytest.mark.fast
def test_status_is_proposed(text: str) -> None:
    """The record is a proposal, not a maintainer-accepted decision."""
    assert re.search(r"\*\*Status:\*\*\s*Proposed", text), "ADR 0008 must stay Proposed"


@pytest.mark.unit
@pytest.mark.fast
def test_relative_file_links_resolve(text: str) -> None:
    """Every relative file link must point at a path that exists here.

    This fails the build if the ADR links a planned-but-unmerged source file
    (for example a proxy that is not on ``main``) as though it already exists.
    """
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    checked = 0
    for match in link_pattern.finditer(text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part, _, _anchor = target.partition("#")
        if not path_part:
            continue
        resolved = (_ADR_DIR / path_part).resolve()
        assert resolved.exists(), f"Broken relative link target in ADR 0008: {target}"
        checked += 1
    assert checked > 0, "Expected at least one intra-repo file link to validate"


@pytest.mark.unit
@pytest.mark.fast
def test_unmerged_surfaces_are_not_asserted_as_built(text: str) -> None:
    """Planned surfaces must not be stated as already-built, present-tense code.

    The ADR analyzes a shared-token residual on surfaces that are not on
    ``main``. Describing that residual is required; asserting the surfaces are
    "as built" or "the existing"/"the real" contract is the drift this guard
    forbids, because the Operations Contracts and Identity docs do not yet
    contain them.
    """
    forbidden = [
        "as built on the e1/e2 branches",
        "the real capacity prepare contract",
        "the existing `operations-capability-discovery`",
    ]
    lowered = text.lower()
    for phrase in forbidden:
        assert phrase not in lowered, f"ADR 0008 asserts an unmerged surface as built: {phrase!r}"


@pytest.mark.unit
@pytest.mark.fast
def test_planned_surfaces_are_marked_proposed_or_tracked(text: str) -> None:
    """The dependency on unmerged work must be visible to a reader of main."""
    lowered = text.lower()
    assert "proposed" in lowered
    # The capacity prepare body and the discovery reuse must be named as
    # planned / dependent on named issues, not as present contracts.
    assert "planned" in lowered
