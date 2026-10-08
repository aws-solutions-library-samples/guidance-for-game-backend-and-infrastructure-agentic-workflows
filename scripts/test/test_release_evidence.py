"""Strict evidence-comment schema validation contracts."""

# Standard library
import json
import sys
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Local modules
from release import evidence  # noqa: E402

COMMIT = "b" * 40


def _good_document(commit=COMMIT, overrides=None):
    gates = {name: "pass" for name in evidence.REQUIRED_GATES}
    doc = {
        "schema_version": evidence.EVIDENCE_SCHEMA_VERSION,
        "commit": commit,
        "gates": gates,
    }
    if overrides:
        doc.update(overrides)
    return doc


def _comment(doc):
    return f"Deployed validation evidence:\n\n```json\n{json.dumps(doc, indent=2)}\n```\n"


class EvidenceExtractionTests(unittest.TestCase):
    def test_requires_exactly_one_json_block(self):
        doc = _good_document()
        body = _comment(doc) + "\n```json\n{}\n```\n"
        with self.assertRaises(evidence.EvidenceError):
            evidence.extract_json_block(body)

    def test_missing_block_rejected(self):
        with self.assertRaises(evidence.EvidenceError):
            evidence.extract_json_block("no fenced block here")


class EvidenceSchemaTests(unittest.TestCase):
    def test_accepts_well_formed_all_pass(self):
        doc = evidence.parse_and_validate(_comment(_good_document()), expected_commit=COMMIT)
        self.assertEqual(doc.commit, COMMIT)
        self.assertTrue(all(status == "pass" for status in doc.gates.values()))

    def test_commit_mismatch_rejected(self):
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(_good_document()), expected_commit="c" * 40)

    def test_unknown_keys_rejected(self):
        doc = _good_document(overrides={"extra": "nope"})
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_wrong_schema_version_rejected(self):
        doc = _good_document(overrides={"schema_version": "gbaw.release-evidence.v2"})
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_missing_gate_rejected(self):
        doc = _good_document()
        doc["gates"].pop(evidence.REQUIRED_GATES[0])
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_unknown_gate_rejected(self):
        doc = _good_document()
        doc["gates"]["invented_gate"] = "pass"
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_waived_without_issue_reference_rejected(self):
        doc = _good_document()
        doc["gates"][evidence.REQUIRED_GATES[1]] = "waived"
        # No waivers mapping at all.
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_waived_with_bad_reference_rejected(self):
        doc = _good_document()
        gate = evidence.REQUIRED_GATES[1]
        doc["gates"][gate] = "waived"
        doc["waivers"] = {gate: "see the ticket"}
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_waived_with_issue_reference_accepted(self):
        doc = _good_document()
        gate = evidence.REQUIRED_GATES[1]
        doc["gates"][gate] = "waived"
        doc["waivers"] = {gate: "#412"}
        parsed = evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)
        self.assertEqual(parsed.gates[gate], "waived")
        self.assertEqual(parsed.waivers[gate], "#412")

    def test_invalid_status_rejected(self):
        doc = _good_document()
        doc["gates"][evidence.REQUIRED_GATES[0]] = "fail"
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(_comment(doc), expected_commit=COMMIT)

    def test_oversize_block_rejected(self):
        doc = _good_document(overrides={"commit": COMMIT})
        body = "```json\n" + ("x" * 20000) + "\n```"
        with self.assertRaises(evidence.EvidenceError):
            evidence.extract_json_block(body)

    def test_non_object_root_rejected(self):
        body = "```json\n[1, 2, 3]\n```"
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(body, expected_commit=COMMIT)


if __name__ == "__main__":
    unittest.main()
