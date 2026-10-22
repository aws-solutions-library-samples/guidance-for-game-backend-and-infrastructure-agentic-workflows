"""Deterministic manifest and operations-mode proof contracts."""

# Standard library
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Local modules
from release import manifest  # noqa: E402

COMMIT = "a" * 40
COMMIT_DATE = "2024-01-02T03:04:05Z"


def _fake_repo(root: Path, *, mode_line: str | None = None, extra_script: str | None = None):
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
        "The default is `disabled`.\n", encoding="utf-8"
    )
    (root / "backend" / "src" / "agents").mkdir(parents=True)
    (root / "backend" / "src" / "agents" / "optimized_prompts.py").write_text("PROMPT = 'x'\n", encoding="utf-8")
    (root / "backend" / "uv.lock").write_text("lock\n", encoding="utf-8")
    (root / "backend" / "requirements.txt").write_text("req\n", encoding="utf-8")
    (root / "ui").mkdir()
    (root / "ui" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    infra = root / "infrastructure" / "cloudformation"
    infra.mkdir(parents=True)
    template = "Resources: {}\n"
    if mode_line:
        template += mode_line + "\n"
    (infra / "01-base-infrastructure.yaml").write_text(template, encoding="utf-8")
    if extra_script:
        scripts = root / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "deploy.sh").write_text(extra_script + "\n", encoding="utf-8")


def _build(root: Path):
    checks = [manifest.CheckRunResult(name="backend-tests", conclusion="success")]
    return manifest.build_manifest(
        root,
        tag="v0.1.0",
        release_name="v0.1.0",
        commit=COMMIT,
        commit_date=COMMIT_DATE,
        required_checks=checks,
        evidence_digest="d" * 64,
        gate_summary={"a_gate": "pass"},
        attached_file_names=["SHA256SUMS", "release-manifest.json"],
    )


class ManifestDeterminismTests(unittest.TestCase):
    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root)
            first = manifest.serialize_manifest(_build(root))
            second = manifest.serialize_manifest(_build(root))
            self.assertEqual(first, second)

    def test_serialization_rules(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root)
            data = manifest.serialize_manifest(_build(root))
            self.assertTrue(data.endswith(b"\n"))
            self.assertNotIn(b"\r", data)
            self.assertEqual(data.decode("ascii"), data.decode("utf-8"))  # ASCII only
            parsed = json.loads(data)
            self.assertEqual(parsed["commit_date"], COMMIT_DATE)

    def test_required_keys_present(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root)
            m = _build(root)
            for key in [
                "schema_version",
                "tag",
                "commit",
                "commit_date",
                "default_operations_mode",
                "hashes",
                "required_checks",
                "evidence",
                "attached_files",
            ]:
                self.assertIn(key, m)
            self.assertEqual(m["default_operations_mode"], "disabled")
            self.assertIn("dependency_locks", m["hashes"])
            self.assertIn("prompt_sources", m["hashes"])
            self.assertIn("infrastructure_templates", m["hashes"])

    def test_only_timestamp_is_commit_date(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root)
            data = manifest.serialize_manifest(_build(root)).decode("utf-8")
            # No other ISO timestamp beyond the commit date.
            self.assertEqual(data.count(COMMIT_DATE), 1)
            self.assertNotIn('Z",', data.replace(COMMIT_DATE, ""))


class OperationsModeProofTests(unittest.TestCase):
    def test_passes_when_nothing_sets_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root)
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")

    def test_fails_when_template_sets_non_disabled_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root, mode_line="  Value: GBAW_OPERATIONS_MODE=operate")
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_fails_when_script_sets_non_disabled_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root, extra_script='export GBAW_OPERATIONS_MODE="remediate"')
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_fails_when_script_references_mode(self):
        # Any reference to GBAW_OPERATIONS_MODE in a deployment script fails
        # closed: the mode is unimplemented and must not appear outside docs,
        # tests, and the release tooling.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root, extra_script='export GBAW_OPERATIONS_MODE="disabled"')
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_fails_when_adr_does_not_declare_default(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fake_repo(root)
            (root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
                "no declaration here\n", encoding="utf-8"
            )
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)


if __name__ == "__main__":
    unittest.main()
