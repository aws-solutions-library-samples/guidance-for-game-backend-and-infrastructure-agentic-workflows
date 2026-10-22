"""Pre-write validation contracts using an in-memory GitHub client."""

# Standard library
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Third-party packages
from _release_fakes import RecordingClient, completed_check  # noqa: E402

# Local modules
from release import evidence, validate  # noqa: E402

COMMIT = "a" * 40


def _evidence_comment(commit=COMMIT):
    gates = {name: "pass" for name in evidence.REQUIRED_GATES}
    doc = {
        "schema_version": evidence.EVIDENCE_SCHEMA_VERSION,
        "commit": commit,
        "gates": gates,
    }
    return f"```json\n{json.dumps(doc, indent=2)}\n```"


def _ready_client(commit=COMMIT):
    client = RecordingClient()
    client.main_head = commit
    client.tags = ["v0.0.1"]
    client.branch_rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": name, "integration_id": 15368} for name in validate.REQUIRED_CHECK_NAMES
                ],
            },
        }
    ]
    for name in validate.REQUIRED_CHECK_NAMES:
        client.check_runs[name] = [completed_check(name, commit)]
    client.comments[55] = {
        "author_association": "OWNER",
        "user": {"login": "maintainer"},
        "issue_url": f"https://api.github.com/repos/{client.owner}/{client.repo}/issues/537",
        "body": _evidence_comment(commit),
    }
    client.collaborator_permissions = {"maintainer": "admin"}
    return client


def _no_findings_comment(_body):
    return []


def _no_findings_files(_paths):
    return []


class CommitMainBindingTests(unittest.TestCase):
    def test_commit_must_equal_main_head(self):
        client = _ready_client()
        client.main_head = "f" * 40
        with self.assertRaises(validate.ValidationError):
            validate.validate_commit_is_main_head(client, COMMIT)

    def test_commit_equal_main_head_passes(self):
        client = _ready_client()
        validate.validate_commit_is_main_head(client, COMMIT)  # no raise


class TagAvailabilityTests(unittest.TestCase):
    def test_existing_tag_ref_rejected(self):
        client = _ready_client()
        client.existing_refs["tags/v0.1.0"] = "deadbeef"
        # Local modules
        from release import semver

        with self.assertRaises(validate.ValidationError):
            validate.validate_tag_available(client, semver.parse_tag("v0.1.0"))

    def test_existing_draft_release_rejected(self):
        client = _ready_client()
        client.releases.append({"tag_name": "v0.1.0", "draft": True})
        # Local modules
        from release import semver

        with self.assertRaises(validate.ValidationError):
            validate.validate_tag_available(client, semver.parse_tag("v0.1.0"))

    def test_older_or_reused_tag_rejected(self):
        client = _ready_client()
        client.tags = ["v0.1.0"]  # candidate equals existing -> reuse of deleted name
        # Local modules
        from release import semver

        with self.assertRaises(validate.ValidationError):
            validate.validate_tag_available(client, semver.parse_tag("v0.1.0"))

    def test_available_greater_tag_passes(self):
        client = _ready_client()
        # Local modules
        from release import semver

        validate.validate_tag_available(client, semver.parse_tag("v0.1.0"))  # no raise


class RequiredCheckTests(unittest.TestCase):
    def test_missing_check_rejected(self):
        client = _ready_client()
        del client.check_runs["codeql-required"]
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_failed_check_rejected(self):
        client = _ready_client()
        client.check_runs["backend-tests"] = [completed_check("backend-tests", COMMIT, conclusion="failure")]
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_check_on_other_sha_rejected(self):
        client = _ready_client()
        client.check_runs["e2e-tests"] = [completed_check("e2e-tests", "0" * 40)]
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_wrong_app_rejected(self):
        client = _ready_client()
        client.check_runs["frontend-tests"] = [completed_check("frontend-tests", COMMIT, app_id=99999)]
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_rerun_latest_success_wins(self):
        client = _ready_client()
        client.check_runs["vulnerability-scan"] = [
            completed_check("vulnerability-scan", COMMIT, conclusion="failure", completed_at="2024-01-01T00:00:00Z"),
            completed_check("vulnerability-scan", COMMIT, conclusion="success", completed_at="2024-01-02T00:00:00Z"),
        ]
        # Latest (later completed_at) is success -> passes.
        validate.validate_required_checks(client, COMMIT)

    def test_all_present_and_success_passes(self):
        client = _ready_client()
        conclusions = validate.validate_required_checks(client, COMMIT)
        self.assertEqual(len(conclusions), len(validate.REQUIRED_CHECK_NAMES))


class EvidenceBindingTests(unittest.TestCase):
    def test_untrusted_author_rejected(self):
        client = _ready_client()
        # Author lacks write permission on the repository.
        client.collaborator_permissions = {"maintainer": "read"}
        with self.assertRaises(validate.ValidationError):
            validate.validate_evidence_comment(client, comment_id=55, commit=COMMIT, scan_fn=_no_findings_comment)

    def test_comment_on_other_repo_rejected(self):
        client = _ready_client()
        client.comments[55]["issue_url"] = "https://api.github.com/repos/other/other/issues/1"
        with self.assertRaises(validate.ValidationError):
            validate.validate_evidence_comment(client, comment_id=55, commit=COMMIT, scan_fn=_no_findings_comment)

    def test_permalink_issue_mismatch_rejected(self):
        client = _ready_client()
        with self.assertRaises(validate.ValidationError):
            validate.validate_evidence_comment(
                client, comment_id=55, commit=COMMIT, scan_fn=_no_findings_comment, expected_issue=999
            )

    def test_scanner_violation_rejected(self):
        client = _ready_client()
        with self.assertRaises(validate.ValidationError):
            validate.validate_evidence_comment(client, comment_id=55, commit=COMMIT, scan_fn=lambda _b: ["account-id"])

    def test_valid_evidence_returns_digest_gates_and_waivers(self):
        client = _ready_client()
        digest, gates, waivers = validate.validate_evidence_comment(
            client, comment_id=55, commit=COMMIT, scan_fn=_no_findings_comment, expected_issue=537
        )
        self.assertEqual(len(digest), 64)
        self.assertEqual(set(gates), set(evidence.REQUIRED_GATES))
        self.assertEqual(waivers, {})  # an all-pass comment grants no waivers


class ReleaseNotesTests(unittest.TestCase):
    def _write_notes(self, root: Path, tag: str, body: str):
        notes = root / "docs" / "releases" / f"{tag}.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(body, encoding="utf-8")

    def test_missing_notes_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(validate.ValidationError):
                validate.validate_release_notes(Path(d), "v0.1.0", scan_fn=_no_findings_files)

    def test_missing_section_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write_notes(root, "v0.1.0", "# v0.1.0\n## Defaults\n## Capabilities\n")
            with self.assertRaises(validate.ValidationError):
                validate.validate_release_notes(root, "v0.1.0", scan_fn=_no_findings_files)

    def test_complete_notes_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            body = "\n".join(
                [
                    "# v0.1.0",
                    "## Defaults",
                    "## Capabilities",
                    "## Known issues",
                    "## Compatibility",
                    "## Deployment",
                    "## Rollback",
                ]
            )
            self._write_notes(root, "v0.1.0", body)
            validate.validate_release_notes(root, "v0.1.0", scan_fn=_no_findings_files)


if __name__ == "__main__":
    unittest.main()
