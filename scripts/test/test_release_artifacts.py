"""Checksum format/determinism/tamper and artifact exclusion-guard contracts."""

# Standard library
import sys
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Local modules
from release import artifact_guard, checksums  # noqa: E402
from release.artifact_guard import Artifact  # noqa: E402


class ChecksumTests(unittest.TestCase):
    def test_format_sorted_sha256sum_style(self):
        data = checksums.build_sha256sums({"b.txt": b"B", "a.txt": b"A"})
        lines = data.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("  a.txt"))
        self.assertTrue(lines[1].endswith("  b.txt"))
        for line in lines:
            digest = line.split("  ", 1)[0]
            self.assertEqual(len(digest), 64)
        self.assertTrue(data.endswith(b"\n"))

    def test_determinism(self):
        files = {"x": b"1", "y": b"2"}
        self.assertEqual(checksums.build_sha256sums(files), checksums.build_sha256sums(dict(files)))

    def test_verify_passes_for_matching(self):
        files = {"a": b"A", "b": b"B"}
        sums = checksums.build_sha256sums(files)
        checksums.verify_against_sums(sums, files)  # no raise

    def test_tamper_detected(self):
        files = {"a": b"A", "b": b"B"}
        sums = checksums.build_sha256sums(files)
        tampered = dict(files)
        tampered["a"] = b"A-modified"
        with self.assertRaises(ValueError):
            checksums.verify_against_sums(sums, tampered)

    def test_missing_file_detected(self):
        files = {"a": b"A", "b": b"B"}
        sums = checksums.build_sha256sums(files)
        with self.assertRaises(ValueError):
            checksums.verify_against_sums(sums, {"a": b"A"})

    def test_extra_file_detected(self):
        files = {"a": b"A"}
        sums = checksums.build_sha256sums(files)
        with self.assertRaises(ValueError):
            checksums.verify_against_sums(sums, {"a": b"A", "c": b"C"})


def _safe_set():
    return [
        Artifact(name=artifact_guard.MANIFEST_NAME, data=b'{"ok": 1}\n'),
        Artifact(name=artifact_guard.BACKEND_SBOM_NAME, data=b'{"spdx": "backend"}\n'),
        Artifact(name=artifact_guard.FRONTEND_SBOM_NAME, data=b'{"spdx": "frontend"}\n'),
        Artifact(name=artifact_guard.CHECKSUMS_NAME, data=b"abc  release-manifest.json\n"),
    ]


class ExclusionGuardTests(unittest.TestCase):
    def test_safe_set_passes(self):
        artifact_guard.assert_artifacts_safe(_safe_set())

    def test_forbidden_name_rejected(self):
        artifacts = _safe_set()
        artifacts.append(Artifact(name="deploy-package.zip", data=b"PK\x03\x04stuff"))
        with self.assertRaises(artifact_guard.ArtifactRejected):
            artifact_guard.assert_artifacts_safe(artifacts)

    def test_source_archive_rejected_even_if_named_allowlisted(self):
        artifacts = _safe_set()
        # Replace manifest content with zip magic bytes.
        artifacts[0] = Artifact(name=artifact_guard.MANIFEST_NAME, data=b"PK\x03\x04 zip body")
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertFalse(result.ok)
        self.assertTrue(any("archive" in v for v in result.violations))

    def test_arn_rejected(self):
        artifacts = _safe_set()
        artifacts[0] = Artifact(
            name=artifact_guard.MANIFEST_NAME,
            data=b'{"role": "arn:aws:iam::123456789012:role/x"}\n',
        )
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertIn(f"{artifact_guard.MANIFEST_NAME}:arn", result.violations)

    def test_nonsynthetic_account_id_rejected(self):
        hostile = ("9" * 12).encode()
        artifacts = _safe_set()
        artifacts[0] = Artifact(name=artifact_guard.MANIFEST_NAME, data=b'{"a": "' + hostile + b'"}\n')
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertIn(f"{artifact_guard.MANIFEST_NAME}:account-id", result.violations)

    def test_synthetic_account_id_allowed(self):
        artifacts = _safe_set()
        artifacts[0] = Artifact(name=artifact_guard.MANIFEST_NAME, data=b'{"a": "123456789012"}\n')
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertNotIn(f"{artifact_guard.MANIFEST_NAME}:account-id", result.violations)

    def test_private_registry_host_rejected(self):
        artifacts = _safe_set()
        artifacts[1] = Artifact(
            name=artifact_guard.BACKEND_SBOM_NAME,
            data=b'{"url": "https://123456789012.dkr.ecr.us-west-2.amazonaws.com/x"}\n',
        )
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertTrue(any(artifact_guard.BACKEND_SBOM_NAME in v for v in result.violations))

    def test_public_registry_host_allowed(self):
        artifacts = _safe_set()
        artifacts[1] = Artifact(
            name=artifact_guard.BACKEND_SBOM_NAME,
            data=b'{"url": "https://files.pythonhosted.org/packages/x.whl"}\n',
        )
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertTrue(result.ok, msg=str(result.violations))

    def test_credential_rejected(self):
        token = b"ghp_" + (b"a" * 36)
        artifacts = _safe_set()
        artifacts[0] = Artifact(name=artifact_guard.MANIFEST_NAME, data=b'{"t": "' + token + b'"}\n')
        result = artifact_guard.guard_artifacts(artifacts)
        self.assertIn(f"{artifact_guard.MANIFEST_NAME}:github-token", result.violations)

    def test_missing_required_artifact_rejected(self):
        artifacts = _safe_set()[:-1]  # drop SHA256SUMS
        with self.assertRaises(artifact_guard.ArtifactRejected):
            artifact_guard.assert_artifacts_safe(artifacts)


if __name__ == "__main__":
    unittest.main()
