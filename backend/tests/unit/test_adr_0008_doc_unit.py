"""Documentation contract tests for ADR 0008 and its threat-model boundary (issue #555).

These are *documentation* guards, not runtime behavior tests. They fail the
build when the chat-proposal handoff record and the threat model's O9 boundary
drift from invariants a reader of the default-deployment source depends on:

1. Referential: every intra-repo relative file link and same-document anchor the
   ADR and the O9/OP-H section use must resolve, so the record cannot cite a
   path or heading that does not exist in this checkout.
2. Grounding: the record describes work delivered by the optional operations
   phase branches, not by the default deployment. Surfaces the record depends on
   (the capacity prepare contract and the discovery contract) must be written as
   a dependency on the owning phase, never asserted as an already-shipped,
   present-tense contract.
3. Semantics: the token-audience gate is a precondition for *any*
   write-adjacent or control route, carried in every location that frames it
   (the ADR context, decision 1, and Implementation child 1; the threat model
   Status banner, Phase Model, OP-H7, and the P1 list), and the chat-renderer
   requirement is stated as planned, not as current behavior.

Phrase checks normalize whitespace and drop Markdown emphasis first, so a
re-wrapped paragraph or a bold/italic marker cannot silently disable a guard.
The gate check keys on the stable phrase "write-adjacent or control route" so a
meaning-preserving reword of the enumerated routes (for example "approve or
reject") does not trip it, while deleting the gate from any one location does.
"""

from __future__ import annotations

# Standard library
import re
from pathlib import Path

# Third-party packages
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS = _REPO_ROOT / "docs"
_ADR_DIR = _DOCS / "adr"
_ADR = _ADR_DIR / "0008-hand-off-chat-proposals-to-direct-approval.md"
_THREAT_MODEL = _DOCS / "THREAT_MODEL.md"


def _normalize(value: str) -> str:
    """Lowercase, drop ``*`` emphasis, and collapse all runs of whitespace.

    Collapsing whitespace means a phrase assertion holds whether or not the
    sentence it belongs to was re-wrapped across lines.
    """
    without_emphasis = value.replace("*", "")
    collapsed = re.sub(r"\s+", " ", without_emphasis)
    return collapsed.lower()


def _slugify(heading: str) -> str:
    """GitHub-flavored anchor slug for a Markdown heading."""
    slug = heading.strip().lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9\-_]", "", slug)


def _slice(text: str, start_marker: str, *end_markers: str) -> str:
    """Return the normalized text from ``start_marker`` up to the next end marker.

    ``start_marker`` and the ``end_markers`` are matched against the normalized
    document, so a re-wrapped heading or sentence still delimits the slice. The
    slice lets a semantic assertion target one specific location (one ADR
    section, one threat-model row) rather than the whole document, so deleting
    the gate from that location fails the guard instead of being masked by the
    same words elsewhere.
    """
    norm = _normalize(text)
    start = norm.find(_normalize(start_marker))
    assert start != -1, f"start marker not found: {start_marker!r}"
    end = len(norm)
    for marker in end_markers:
        found = norm.find(_normalize(marker), start + 1)
        if found != -1:
            end = min(end, found)
    return norm[start:end]


# The seven locations that must each carry the token-audience gate. Each entry
# is (human name, document key, start marker, end marker). Removing the gate
# from any one of these fails ``test_token_audience_gate_in_every_location``.
_GATE_LOCATIONS = (
    ("ADR context", "adr", "Decision 1 therefore makes token-audience", "## Decision"),
    ("ADR decision 1", "adr", "### 1. The operations API must accept only", "Chosen mechanism"),
    ("ADR child 1", "adr", "Operations token-audience separation", "2. **Proposal proxy."),
    ("TM Status banner", "tm", "Status banner.", "## Phase Model"),
    ("TM Phase Model", "tm", "## Phase Model", "## Operations Trust Boundaries"),
    ("TM OP-H7", "tm", "| OP-H7 |", "Deployed today:"),
    ("TM P1 list", "tm", "Before enabling any operations write", "## Review Schedule"),
)

_GATE_PHRASE = "write-adjacent or control"


@pytest.fixture(scope="module")
def adr_text() -> str:
    return _ADR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adr_norm(adr_text: str) -> str:
    return _normalize(adr_text)


@pytest.fixture(scope="module")
def tm_text() -> str:
    return _THREAT_MODEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tm_norm(tm_text: str) -> str:
    return _normalize(tm_text)


@pytest.mark.unit
@pytest.mark.fast
def test_adr_exists(adr_text: str) -> None:
    assert adr_text.strip(), "ADR 0008 must not be empty"


@pytest.mark.unit
@pytest.mark.fast
def test_status_line_is_well_formed(adr_text: str) -> None:
    """The record carries an explicit, recognized status label.

    This does not pin ``Proposed``: the ADR anticipates its own acceptance and a
    later record may supersede it, so the guard only requires that the status
    *begins* with a recognized label. ``Superseded by ADR 0010`` and
    ``Accepted (2026-11-01)`` are therefore both accepted.
    """
    match = re.search(r"\*\*Status:\*\*\s*(.+)", adr_text)
    assert match, "ADR 0008 must declare a status"
    status = match.group(1).strip().lower()
    first_word = re.match(r"[a-z]+", status)
    assert first_word, f"Unexpected ADR 0008 status: {status!r}"
    assert first_word.group(0) in {
        "proposed",
        "accepted",
        "superseded",
        "deprecated",
    }, f"Unexpected ADR 0008 status: {status!r}"


@pytest.mark.unit
@pytest.mark.fast
def test_relative_links_and_anchors_resolve(adr_text: str, tm_text: str) -> None:
    """Every relative file link and same-document anchor in the ADR and the
    threat model must resolve, mirroring ``test_threat_model_doc_unit.py``.

    This fails the build if the record links a path that does not exist here or
    an anchor that a referenced document does not define.
    """
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for doc_path, text in ((_ADR, adr_text), (_THREAT_MODEL, tm_text)):
        base_dir = doc_path.parent
        self_anchors = {_slugify(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", text, flags=re.MULTILINE)}
        checked = 0
        for match in link_pattern.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, anchor = target.partition("#")
            if path_part == "":
                assert anchor in self_anchors, f"{doc_path.name}: dangling same-doc anchor #{anchor}"
                checked += 1
                continue
            resolved = (base_dir / path_part).resolve()
            assert resolved.exists(), f"{doc_path.name}: broken relative link target {target}"
            checked += 1
            if anchor:
                body = resolved.read_text(encoding="utf-8")
                anchors = {_slugify(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", body, flags=re.MULTILINE)}
                assert anchor in anchors, f"{doc_path.name}: broken anchor in {path_part}: #{anchor}"
        assert checked > 0, f"{doc_path.name}: expected at least one intra-repo link to validate"


@pytest.mark.unit
@pytest.mark.fast
def test_unmerged_surfaces_are_not_asserted_as_shipped(adr_norm: str, tm_norm: str) -> None:
    """Phase-branch surfaces must not be stated as already-shipped contracts,
    and the early-enablement gap must not be reintroduced as "accepted".

    Each phrase here asserts an operations surface the default deployment does
    not provide as though it already exists, or softens the gate into an
    accepted gap, or restores the overstated approver rule. Whitespace and
    emphasis are normalized first, so a re-wrapped or bolded sentence cannot slip
    a forbidden phrase past the guard.
    """
    forbidden = [
        "as built on the e1/e2 branches",
        "as built on the operations branches",
        "as built on the e1-e4 branches",
        "the existing `operations-capability-discovery`",
        "the existing operations-capability-discovery",
        "the existing cancel proxy",
        "the shipped cancel proxy",
        "the real capacity prepare contract",
        "the existing capacity prepare contract",
        # The gate is a precondition, never a silently or explicitly accepted gap.
        "explicitly accepted gap",
        "silently accepted gap",
        # The approver rule defers to policy; it is not an unconditional
        # separation of the approver from the requester.
        "a distinct `admin` must approve",
        "a distinct admin must approve",
        "distinct from the requester",
    ]
    for phrase in forbidden:
        needle = _normalize(phrase)
        assert needle not in adr_norm, f"ADR 0008 asserts a forbidden phrase: {phrase!r}"
        assert needle not in tm_norm, f"THREAT_MODEL O9 asserts a forbidden phrase: {phrase!r}"


@pytest.mark.unit
@pytest.mark.fast
def test_dependency_grounding_is_visible(adr_norm: str) -> None:
    """The dependency on the operations phases is named, not implied."""
    # The capacity prepare body and the discovery contract are named as
    # dependencies on the owning phases rather than as published contracts.
    assert "gamelift-capacity-proposal-request" in adr_norm
    assert "operations-capability-discovery" in adr_norm
    assert "outside the nine source-control contracts" in adr_norm


@pytest.mark.unit
@pytest.mark.fast
def test_token_audience_gate_in_every_location(adr_text: str, tm_text: str) -> None:
    """The gate must appear in each location that frames it.

    Keying on the stable phrase ``write-adjacent or control route`` means a
    meaning-preserving reword of the enumerated routes (``approve or reject``,
    ``approval, rejection, dispatch``) still passes, while deleting the gate
    from the context, decision 1, child 1, the Status banner, the Phase Model,
    OP-H7, or the P1 list fails here rather than being masked by the same words
    elsewhere in the document.
    """
    docs = {"adr": adr_text, "tm": tm_text}
    for name, doc_key, start_marker, end_marker in _GATE_LOCATIONS:
        section = _slice(docs[doc_key], start_marker, end_marker)
        assert _GATE_PHRASE in section, f"{name}: token-audience gate ('{_GATE_PHRASE} route') is missing"
    # And the ADR frames it explicitly as a precondition in the decision.
    decision = _slice(adr_text, "### 1. The operations API must accept only", "Chosen mechanism")
    assert "precondition" in decision, "ADR decision 1 must frame the gate as a precondition"


@pytest.mark.unit
@pytest.mark.fast
def test_renderer_requirement_is_planned_not_current(adr_norm: str) -> None:
    """The chat renderer does not neutralize model links today.

    Stating neutralization as current behavior is wrong (the default renderer
    makes model links clickable), so the record must frame it as a requirement.
    """
    assert "neutralize" in adr_norm or "neutralizes" in adr_norm, "ADR 0008 must require link neutralization"
    assert (
        "treats model output as text, not navigation" not in adr_norm
    ), "renderer premise must not be stated as current behavior"


@pytest.mark.unit
@pytest.mark.fast
def test_op_h_rows_present(tm_text: str) -> None:
    """The O9 boundary and every OP-H threat row must stay present as table rows.

    Matching the row start ``| OP-Hn |`` rather than any mention of the ID means
    deleting a row fails here even though the version-history prose still names
    the range ``OP-H1 through OP-H7``.
    """
    assert "O9" in tm_text and "Chat proposal handoff" in tm_text
    for threat_id in ("OP-H1", "OP-H2", "OP-H3", "OP-H4", "OP-H5", "OP-H6", "OP-H7"):
        assert re.search(
            rf"^\|\s*{re.escape(threat_id)}\s*\|", tm_text, flags=re.MULTILINE
        ), f"Threat row {threat_id} must be present as a table row in the threat model"
