#!/usr/bin/env python3
"""Smoke test: the primary architecture and threat-model docs describe the connector.

These are fast text assertions over the repo-root ``docs/ARCHITECTURE.md`` and
``docs/THREAT_MODEL.md`` (no AWS, no imports of application code). They pin the R13 / MR7
documentation invariant: BOTH docs must describe the Source Control Connector's five
reviewer-facing concerns —

* the **external provider trust boundary** (outbound HTTPS to a third-party source-control
  provider, outside the AWS/IAM trust domain),
* the **write credential** (a single Secrets Manager ARN, adapter-owned),
* the **authorization policy** (five dimensions — repository, branch, path, extension, group —
  enforced on reads and writes),
* the **audit flow** (durable intent/outcome events with reconciliation, and NO cross-system
  atomicity claim), and
* the **human-review gate** (only unmerged proposals; no merge/approve/close/delete/force-push).

Each check runs against the connector *section* of each doc (not the whole file) using
case-insensitive substring/regex probes for the key concepts, so the assertions are robust to
wording and punctuation. A dedicated **negative** assertion guards the deliberate Task 6
reversal: neither connector section may present cross-system atomicity as a guarantee — instead
each must carry the explicit no-atomicity framing (a negated ``atomic`` phrase) alongside the
reconciliation story. This proves the reversal is *documented*, not contradicted.

The test mirrors ``test_iam_scm_credential_smoke.py`` / ``test_deploy_scm_wiring_smoke.py`` in
how it locates the repo-root files.

Validates: Requirements 13.1, 13.2
"""

# Standard library
import re
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


# --- Locate the docs ----------------------------------------------------------------------

# tests/unit/<this file> -> tests/unit -> tests -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS_DIR = _REPO_ROOT / "docs"
_ARCHITECTURE_PATH = _DOCS_DIR / "ARCHITECTURE.md"
_THREAT_MODEL_PATH = _DOCS_DIR / "THREAT_MODEL.md"

# The top-level connector heading both docs open the section with. ARCHITECTURE.md adds a
# parenthetical suffix ("(Optional Write Path)"); THREAT_MODEL.md uses the bare name.
_CONNECTOR_HEADING_RE = re.compile(r"^##\s+Source Control Connector\b", re.MULTILINE)


# --- Section extraction -------------------------------------------------------------------


def _connector_section(text: str) -> str:
    """Return the connector section: from its ``## Source Control Connector`` heading up to
    the next top-level (``## ``) heading. Sub-headings (``### ``) stay inside the section."""
    match = _CONNECTOR_HEADING_RE.search(text)
    assert match is not None, "no '## Source Control Connector' section heading found"
    start = match.start()
    # Find the next top-level heading after the connector heading line.
    next_top = re.search(r"^##\s+(?!#)", text[match.end() :], re.MULTILINE)
    end = match.end() + next_top.start() if next_top else len(text)
    section = text[start:end]
    assert len(section) > 200, "connector section is unexpectedly short"
    return section


# --- Fixtures -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def architecture_section() -> str:
    assert _ARCHITECTURE_PATH.is_file(), f"doc not found at {_ARCHITECTURE_PATH}"
    return _connector_section(_ARCHITECTURE_PATH.read_text()).lower()


@pytest.fixture(scope="module")
def threat_model_section() -> str:
    assert _THREAT_MODEL_PATH.is_file(), f"doc not found at {_THREAT_MODEL_PATH}"
    return _connector_section(_THREAT_MODEL_PATH.read_text()).lower()


# --- Concept probes (case-insensitive; run against the lowered section text) --------------

_FIVE_DIMENSIONS = ("repository", "branch", "path", "extension", "group")


def _has_all(text: str, *needles: str) -> bool:
    """True iff every needle (already lowercased) is a substring of text."""
    return all(n in text for n in needles)


def _has_any(text: str, *patterns: str) -> bool:
    """True iff any regex pattern matches text."""
    return any(re.search(p, text) is not None for p in patterns)


def _assert_trust_boundary(section: str, doc: str) -> None:
    """Outbound HTTPS to a third-party provider, framed as a trust boundary."""
    assert "outbound https" in section, f"{doc}: missing outbound-HTTPS description"
    assert "trust boundary" in section, f"{doc}: missing 'trust boundary' framing"
    assert _has_any(section, r"third[\s-]party"), f"{doc}: connector section must call out a third-party provider"


def _assert_write_credential(section: str, doc: str) -> None:
    """A single Secrets Manager ARN, adapter-owned."""
    assert "secrets manager" in section, f"{doc}: missing 'Secrets Manager' credential store"
    assert "arn" in section, f"{doc}: missing ARN reference for the write credential"
    assert "adapter" in section, f"{doc}: credential must be described as adapter-owned"


def _assert_authorization_policy(section: str, doc: str) -> None:
    """Five dimensions (repository, branch, path, extension, group) on reads and writes."""
    assert "five" in section, f"{doc}: authorization must be described as five-dimension"
    missing = [d for d in _FIVE_DIMENSIONS if d not in section]
    assert not missing, f"{doc}: authorization section missing dimension(s): {missing}"
    assert _has_any(section, r"read", r"write"), f"{doc}: authorization must reference reads/writes"


def _assert_audit_flow(section: str, doc: str) -> None:
    """Durable intent + outcome events reconciled (not atomic)."""
    assert "intent" in section, f"{doc}: audit flow must mention intent events"
    assert "outcome" in section, f"{doc}: audit flow must mention outcome events"
    assert _has_any(section, r"reconcil"), f"{doc}: audit flow must describe reconciliation of ambiguous outcomes"


def _assert_human_review_gate(section: str, doc: str) -> None:
    """Only unmerged proposals; no merge/approve/close operation exposed."""
    assert "unmerged" in section, f"{doc}: human-review gate must describe unmerged proposals"
    assert _has_any(
        section,
        r"no merge",
        r"cannot merge",
        r"never merge",
        r"merge\s*[/,]\s*approve",  # "merge/approve/close" or "merge, approve, close"
        r"(?:not|no|never|cannot)\b[^.]{0,60}\bmerge",
    ), f"{doc}: human-review gate must state no merge/approve/close capability"


def _assert_no_atomicity_claim(section: str, doc: str) -> None:
    """NEGATIVE guard for the Task 6 reversal.

    The connector section must NOT present cross-system atomicity as a guarantee. Because the
    docs legitimately use the word "atomic" (in the phrase that *denies* atomicity), we do not
    fail on the substring alone; instead we require the explicit no-atomicity framing — a
    negated ``atomic`` phrase (e.g. "does not claim ... atomicity", "instead of ... atomicity",
    "overclaimed atomicity") — together with the reconciliation story that replaces it.
    """
    assert "atomic" in section, f"{doc}: expected the (denied) atomicity framing to be present"
    negated_atomicity = _has_any(
        section,
        r"(?:not|no|never|does not|without|instead of|overclaim\w*)\b[^.]{0,60}\batomic",
        r"\batomic\w*\b[^.]{0,60}\b(?:not\b|claim)",  # "atomicity ... claim" / "... not"
    )
    assert negated_atomicity, (
        f"{doc}: connector section must explicitly disclaim cross-system atomicity, " "not present it as a guarantee"
    )
    assert _has_any(section, r"reconcil"), f"{doc}: the no-atomicity framing must be paired with reconciliation"


# --- Tests: ARCHITECTURE.md ---------------------------------------------------------------


def test_architecture_describes_trust_boundary(architecture_section: str):
    """(Req 13.1) ARCHITECTURE.md describes the external provider trust boundary."""
    _assert_trust_boundary(architecture_section, "ARCHITECTURE.md")


def test_architecture_describes_write_credential(architecture_section: str):
    """(Req 13.1) ARCHITECTURE.md describes the single-ARN, adapter-owned write credential."""
    _assert_write_credential(architecture_section, "ARCHITECTURE.md")


def test_architecture_describes_authorization_policy(architecture_section: str):
    """(Req 13.1) ARCHITECTURE.md describes the five-dimension authorization policy."""
    _assert_authorization_policy(architecture_section, "ARCHITECTURE.md")


def test_architecture_describes_audit_flow(architecture_section: str):
    """(Req 13.1) ARCHITECTURE.md describes the durable intent/outcome audit flow."""
    _assert_audit_flow(architecture_section, "ARCHITECTURE.md")


def test_architecture_describes_human_review_gate(architecture_section: str):
    """(Req 13.1) ARCHITECTURE.md describes the human-review gate."""
    _assert_human_review_gate(architecture_section, "ARCHITECTURE.md")


def test_architecture_does_not_claim_cross_system_atomicity(architecture_section: str):
    """(Req 13.1, Task 6 reversal) ARCHITECTURE.md disclaims cross-system atomicity."""
    _assert_no_atomicity_claim(architecture_section, "ARCHITECTURE.md")


# --- Tests: THREAT_MODEL.md ---------------------------------------------------------------


def test_threat_model_describes_trust_boundary(threat_model_section: str):
    """(Req 13.2) THREAT_MODEL.md describes the external provider trust boundary."""
    _assert_trust_boundary(threat_model_section, "THREAT_MODEL.md")


def test_threat_model_describes_write_credential(threat_model_section: str):
    """(Req 13.2) THREAT_MODEL.md describes the single-ARN, adapter-owned write credential."""
    _assert_write_credential(threat_model_section, "THREAT_MODEL.md")


def test_threat_model_describes_authorization_policy(threat_model_section: str):
    """(Req 13.2) THREAT_MODEL.md describes the five-dimension authorization policy."""
    _assert_authorization_policy(threat_model_section, "THREAT_MODEL.md")


def test_threat_model_describes_audit_flow(threat_model_section: str):
    """(Req 13.2) THREAT_MODEL.md describes the durable intent/outcome audit flow."""
    _assert_audit_flow(threat_model_section, "THREAT_MODEL.md")


def test_threat_model_describes_human_review_gate(threat_model_section: str):
    """(Req 13.2) THREAT_MODEL.md describes the human-review gate."""
    _assert_human_review_gate(threat_model_section, "THREAT_MODEL.md")


def test_threat_model_does_not_claim_cross_system_atomicity(threat_model_section: str):
    """(Req 13.2, Task 6 reversal) THREAT_MODEL.md disclaims cross-system atomicity."""
    _assert_no_atomicity_claim(threat_model_section, "THREAT_MODEL.md")
