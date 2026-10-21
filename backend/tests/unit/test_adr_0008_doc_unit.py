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
   Status banner, Phase Model, O9 narrative, OP-H7, the attack-tree leaf, and
   the P1 list), and the chat-renderer requirement is stated as planned, not as
   current behavior.

Phrase checks strip blockquote markers, fold en and em dashes to a
hyphen-minus, drop ``*`` and ``_`` emphasis, and collapse whitespace first, so
a re-wrapped paragraph, a quoted banner, a dash style, or a bold/italic marker
cannot silently disable a guard. The gate check keys on the stable phrase
"write-adjacent or control" so a meaning-preserving reword of the
enumerated routes (for example "approve or reject") does not trip it, while
deleting the gate from any one framing sentence does.
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
    """Lowercase, strip blockquote markers, fold dashes and emphasis, and
    collapse all runs of whitespace.

    Stripping a leading ``>`` quote marker on each line means a phrase assertion
    holds inside a blockquote (the threat model's Status banner) and is not
    tripped by a re-wrap that moves a line break into the middle of the phrase.
    Folding en and em dashes to a hyphen-minus, and dropping ``*`` and ``_``
    emphasis, means a dash or emphasis style cannot silently disable a guard.
    Collapsing whitespace means a phrase assertion holds whether or not the
    sentence it belongs to was re-wrapped across lines.
    """
    without_quotes = re.sub(r"(?m)^\s*>\s?", "", value)
    without_emphasis = without_quotes.replace("*", "").replace("_", "")
    folded_dashes = without_emphasis.replace("\u2014", "-").replace("\u2013", "-")
    collapsed = re.sub(r"\s+", " ", folded_dashes)
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


# The locations that must each carry the token-audience gate. Each entry is
# (human name, document key, start marker, end markers, min_gate_sentences,
# require_precondition). ``min_gate_sentences`` is the number of distinct gate
# sentences the location frames: the ADR context and child 1 each state it twice
# (an opening/title framing and a closing framing), so reverting or deleting
# either one of the two drops the count below the minimum and fails the guard.
# ``require_precondition`` additionally pins the literal word "precondition" in
# the locations that frame the gate as one, so weakening "precondition" to
# "consideration" (or similar) across the threat model fails here too.
_GATE_LOCATIONS = (
    ("ADR context", "adr", "Decision 1 therefore makes token-audience", ("## Decision",), 2, True),
    ("ADR decision 1", "adr", "### 1. The operations API must accept only", ("Chosen mechanism",), 1, True),
    ("ADR child 1", "adr", "Operations token-audience separation", ("2. **Proposal proxy.",), 2, True),
    ("TM Status banner", "tm", "Status banner.", ("## Phase Model",), 1, True),
    ("TM Phase Model", "tm", "## Phase Model", ("## Operations Trust Boundaries",), 1, False),
    (
        "TM O9 narrative",
        "tm",
        "### O9 \u2014 Chat proposal handoff",
        ("## Operations Trust Boundaries", "| OP-H1 |"),
        1,
        True,
    ),
    ("TM OP-H7", "tm", "| OP-H7 |", ("Deployed today:",), 1, True),
    ("TM P1 list", "tm", "Before enabling any operations write", ("## Review Schedule",), 1, False),
)

_GATE_PHRASE = "write-adjacent or control"

# The attack-tree leaf frames the gate with a different route enumeration
# ("approve, reject, dispatch, or control route"), so it is checked for the
# stable precondition clause rather than the ADR/banner phrase. Deleting the
# leaf sentence or rewriting it to drop the precondition framing fails here.
_LEAF_START = "One Cognito audience is shared across the planned operations APIs"
_LEAF_END = "Model output submitted as an authoritative proposal"
_LEAF_GATE = "a precondition for enabling any"


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
        "a distinct approver",
        # Managed login (path (b)) is a planned follow-up for a later ADR, not
        # the path this ADR chooses; path (a) is the chosen path here.
        "chosen path (b)",
        "obtaining the operations token (chosen path (b))",
        # The E2 narrower idempotency fingerprint is a required fix in #429, not
        # an accepted residual or a tolerated deviation recorded by this record.
        "recorded here as a residual",
        "is a deviation from adr 0005's canonical fingerprint",
    ]
    # Scoped to the ADR only: a threat model legitimately describes an executor
    # or automation identity as distinct from the requester and approver (O4
    # says "distinct from requester/approver"; OP-ID1 says "distinct from human
    # requester/approver"), so this approver-rule phrasing is forbidden
    # anywhere in the ADR, which is scanned in full.
    forbidden_adr_only = [
        "distinct from the requester",
    ]
    for phrase in forbidden:
        needle = _normalize(phrase)
        assert needle not in adr_norm, f"ADR 0008 asserts a forbidden phrase: {phrase!r}"
        assert needle not in tm_norm, f"THREAT_MODEL O9 asserts a forbidden phrase: {phrase!r}"
    for phrase in forbidden_adr_only:
        needle = _normalize(phrase)
        assert needle not in adr_norm, f"ADR 0008 asserts a forbidden phrase: {phrase!r}"


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
def test_sign_in_decision_chooses_path_a_and_defers_managed_login(adr_norm: str) -> None:
    """Path (a) is the chosen operations-token path; managed login (path (b)) is
    a planned follow-up for a separate, later ADR after the Cloudscape UI cutover.

    This pins decision 1's sequencing so the record cannot drift back to
    choosing managed login here (the superseded "chosen path (b)") or to folding
    the managed-login migration into this ADR. It guards the same property the
    old pins guarded — the precondition gate and no overstatement of what this
    record changes — now against the decision this ADR makes: the shell cutover
    (#552) and its rollback never change the sign-in flow at the same time.
    """
    # Path (a) is the chosen operations-token path for this ADR.
    assert "chosen path (a)" in adr_norm, "decision 1 must choose path (a) as the operations-token path"
    # Managed login is a separate, later ADR, not decided here, and it lands
    # after the Cloudscape UI cutover (#552), coordinated with the sign-in
    # rebuild (#548).
    assert "path (b)" in adr_norm, "managed login must still be named as the planned follow-up path (b)"
    assert "separate decision recorded by a later adr" in adr_norm, "managed login must be deferred to a later ADR"
    assert "after the cloudscape ui cutover" in adr_norm, "managed login must land after the Cloudscape UI cutover"
    assert "#552" in adr_norm, "the Cloudscape UI cutover issue (#552) must be named"
    assert "#548" in adr_norm, "coordination with the Cloudscape sign-in rebuild (#548) must be named"
    # Implementation child 1 keeps path (a) and points managed login out to the
    # follow-up rather than carrying it.
    child_1 = _slice(
        adr_norm,
        "Operations token-audience separation",
        "2. **Proposal proxy.",
    )
    assert "path (a)" in child_1, "Implementation child 1 must keep path (a)"
    assert (
        "separate adr and issue, after the cloudscape ui cutover" in child_1
    ), "Implementation child 1 must point managed login out to a separate ADR and issue after #552"


@pytest.mark.unit
@pytest.mark.fast
def test_e2_idempotency_must_use_canonical_fingerprint(adr_norm: str, tm_norm: str) -> None:
    """The E2 capacity-prepare path must adopt ADR 0005's canonical fingerprint
    in #429 before E2 merges; it is a required fix, not an accepted residual.

    This replaces the old residual framing with a guard that the record now
    states the fix as required and names the owning issue (#429), so the
    cross-requester replay cannot be normalized as a tolerated deviation in a
    Proposed ADR or an approved threat model. The forbidden-phrase guard removes
    the old "recorded here as a residual" / "deviation from ADR 0005" wording;
    this pins the replacement.
    """
    # The ADR frames the E2 fingerprint as a required fix keyed on ADR 0005's
    # canonical fingerprint, owned by #429 (advise stack in #431).
    assert (
        "does not meet adr 0005's canonical fingerprint" in adr_norm
    ), "the E2 narrower fingerprint must be stated as not meeting ADR 0005's canonical fingerprint"
    assert "before e2 merges" in adr_norm, "the canonical fingerprint must be required before E2 merges"
    assert "#429" in adr_norm, "the E2 idempotency fix must name its owning issue (#429)"
    # The threat model's OP-H5 no longer carries the cross-requester replay as a
    # residual; its residual column is now just the mis-tuning risk, and it
    # points at the required #429 fix.
    assert (
        "cross-requester idempotency replay" not in tm_norm
    ), "OP-H5 must not record cross-requester idempotency replay as a residual risk"
    assert (
        "must adopt that fingerprint in #429" in tm_norm
    ), "OP-H5 must state the E2 path adopts ADR 0005's canonical fingerprint in #429"


@pytest.mark.unit
@pytest.mark.fast
def test_token_audience_gate_in_every_location(adr_text: str, tm_text: str) -> None:
    """The gate must appear in each sentence that frames it, not once per slice.

    Keying on the stable phrase ``write-adjacent or control`` means a
    meaning-preserving reword of the enumerated routes (``approve or reject``,
    ``approval, rejection, dispatch``) still passes. Requiring the phrase as many
    times as the location frames it means deleting *one* of the two gate
    sentences in the ADR context or child 1 (for example reverting only the
    opening sentence or only the child-1 title to a "blocker of E2 activation"
    phrasing) fails here, rather than being masked by the sibling sentence that
    still carries the phrase. The O9 narrative and the attack-tree leaf are
    checked too, so dropping the gate from either of those also fails.
    """
    docs = {"adr": adr_text, "tm": tm_text}
    for name, doc_key, start_marker, end_markers, min_sentences, require_precondition in _GATE_LOCATIONS:
        section = _slice(docs[doc_key], start_marker, *end_markers)
        count = section.count(_GATE_PHRASE)
        assert count >= min_sentences, (
            f"{name}: token-audience gate ('{_GATE_PHRASE}') must appear in "
            f"{min_sentences} framing sentence(s), found {count}"
        )
        if require_precondition:
            assert "precondition" in section, f"{name}: the gate must be framed as a precondition"

    # The attack-tree leaf frames the gate with its own route enumeration.
    leaf = _slice(tm_text, _LEAF_START, _LEAF_END)
    assert _normalize(_LEAF_GATE) in leaf, "TM attack-tree leaf: the gate precondition clause is missing"


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
