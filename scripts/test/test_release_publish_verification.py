"""Publication contracts: publish only the verified, downloaded artifact set.

The ``publish`` job must not rebuild artifacts. It downloads the artifact set
produced by the read-only ``build-artifacts`` job, verifies it against
``SHA256SUMS``, verifies the manifest's tag / commit / attached-file list equal
the dispatch inputs, re-runs the exclusion guard and the pre-write validations,
and verifies the ``release`` environment is protected by at least one
required-reviewers rule -- all before any write.
"""

# Standard library
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Third-party packages
from _release_fakes import RecordingClient  # noqa: E402

# Local modules
from release import artifacts as artifacts_mod  # noqa: E402
from release import publish  # noqa: E402
from release import validate as validate_mod  # noqa: E402

COMMIT = "a" * 40


def _write_artifact_set(out_dir: Path, *, tag="v0.1.0", commit=COMMIT) -> None:
    """Build a real, guarded artifact set on disk, as build-artifacts would."""
    manifest = (
        '{\n  "attached_files": [\n    "SHA256SUMS",\n'
        '    "backend-sbom.spdx.json",\n    "frontend-sbom.spdx.json",\n'
        '    "release-manifest.json"\n  ],\n'
        f'  "commit": "{commit}",\n'
        '  "schema_version": "gbaw.release-manifest.v1",\n'
        f'  "tag": "{tag}"\n}}\n'
    ).encode("utf-8")
    arts, _sums = artifacts_mod.assemble_artifacts(
        manifest_bytes=manifest,
        backend_sbom_bytes=b'{"spdx": "backend"}\n',
        frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for art in arts:
        (out_dir / art.name).write_bytes(art.data)


def _protected_environment():
    return {
        "name": "release",
        "protection_rules": [
            {"id": 1, "type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"login": "octo"}}]}
        ],
    }


def _ready_client(*, environment=None):
    client = RecordingClient()
    client.main_head = COMMIT
    client.environments = {}
    if environment is not None:
        client.environments["release"] = environment
    return client


class LoadFromDirTests(unittest.TestCase):
    def test_load_artifacts_from_dir_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            arts = publish.load_artifacts_from_dir(out)
            names = {a.name for a in arts}
            self.assertEqual(
                names,
                {"release-manifest.json", "backend-sbom.spdx.json", "frontend-sbom.spdx.json", "SHA256SUMS"},
            )

    def test_extra_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            (out / "unexpected.txt").write_bytes(b"nope\n")
            with self.assertRaises(validate_mod.ValidationError):
                publish.load_artifacts_from_dir(out)

    def test_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            (out / "release-manifest.json").unlink()
            with self.assertRaises(validate_mod.ValidationError):
                publish.load_artifacts_from_dir(out)


class PublishFromDirTests(unittest.TestCase):
    def _inputs(self, tag="v0.1.0"):
        return publish.PublishMetadata(
            tag=tag,
            commit=COMMIT,
            release_name=tag,
            notes="# notes\n## Defaults\n",
            tagger_name="Release Bot",
            tagger_email="release@example.com",
            commit_date="2024-01-02T03:04:05Z",
        )

    def test_happy_path_publishes_downloaded_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(environment=_protected_environment())
            result = publish.publish_release_from_dir(client, out, self._inputs())
            self.assertTrue(result.published)
            # No rebuild: the create_release write happened against the set.
            self.assertIn("create_release", client.calls)

    def test_refuses_when_manifest_tag_differs_from_input(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out, tag="v0.9.9")  # manifest says a different tag
            client = _ready_client(environment=_protected_environment())
            with self.assertRaises(Exception):
                publish.publish_release_from_dir(client, out, self._inputs(tag="v0.1.0"))
            self.assertNotIn("create_release", client.calls)

    def test_refuses_when_checksums_tampered(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            # Tamper the manifest after SHA256SUMS was written.
            (out / "release-manifest.json").write_bytes(b'{"tampered": true}\n')
            client = _ready_client(environment=_protected_environment())
            with self.assertRaises(Exception):
                publish.publish_release_from_dir(client, out, self._inputs())
            self.assertNotIn("create_release", client.calls)

    def test_refuses_when_an_sbom_is_tampered_without_touching_the_manifest(self):
        # Editing an SBOM leaves the manifest's self-described identity intact,
        # so the manifest-identity check passes it; only the SHA256SUMS tamper
        # check can reject it. This pins that the checksum verification actually
        # runs: dropping it would let a tampered SBOM through here where the
        # manifest-tamper test above would not notice.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            # Change the SBOM bytes but do not touch SHA256SUMS or the manifest.
            (out / "backend-sbom.spdx.json").write_bytes(b'{"spdx": "backend-TAMPERED"}\n')
            client = _ready_client(environment=_protected_environment())
            with self.assertRaises(Exception):
                publish.publish_release_from_dir(client, out, self._inputs())
            self.assertNotIn("create_release", client.calls)

    def test_publish_from_dir_verifies_the_downloaded_checksums(self):
        # Pin that publish_release_from_dir runs its OWN SHA256SUMS check on the
        # downloaded set (the first of the two defense-in-depth checks). Spying
        # on verify_against_sums counts how many times it runs: both
        # publish_release_from_dir's check and preflight's redundant check must
        # fire, so removing either drops the count below two and fails here.
        # (This counts the checks; it does not assert their position relative to
        # the writes.)
        # Standard library
        from unittest import mock

        # Local modules
        from release import checksums as checksums_mod

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(environment=_protected_environment())
            calls = {"n": 0}
            original = checksums_mod.verify_against_sums

            def spy(sums, covered):
                calls["n"] += 1
                return original(sums, covered)

            with mock.patch.object(publish.checksums, "verify_against_sums", spy):
                publish.publish_release_from_dir(client, out, self._inputs())
            # Two independent checks run: publish_release_from_dir's and preflight's.
            self.assertGreaterEqual(calls["n"], 2, msg=f"expected both checksum checks to run, saw {calls['n']}")

    def test_preflight_verifies_the_checksums_of_its_inputs(self):
        # Pin that preflight independently re-verifies SHA256SUMS against the
        # PublishInputs it is handed (the second defense-in-depth check). A set
        # whose artifact bytes do not match its sha256sums must be rejected by
        # preflight alone, before any write.
        # Local modules
        from release import artifacts as artifacts_mod

        arts, sums = artifacts_mod.assemble_artifacts(
            manifest_bytes=b'{"schema_version": "gbaw.release-manifest.v1"}\n',
            backend_sbom_bytes=b'{"spdx": "backend"}\n',
            frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
        )
        # Tamper one artifact's bytes AFTER sums were computed, so inputs.sha256sums
        # no longer matches inputs.artifacts; only preflight's check can catch it.
        tampered = []
        for art in arts:
            if art.name == "backend-sbom.spdx.json":
                tampered.append(
                    art.__class__(name=art.name, data=b'{"spdx": "TAMPERED"}\n', content_type=art.content_type)
                )
            else:
                tampered.append(art)
        inputs = publish.PublishInputs(
            tag="v0.1.0",
            commit=COMMIT,
            release_name="v0.1.0",
            notes="# notes\n",
            artifacts=tampered,
            sha256sums=sums,
            tagger_name="Bot",
            tagger_email="release@example.com",
            commit_date="2024-01-02T03:04:05Z",
        )
        client = _ready_client(environment=_protected_environment())
        with self.assertRaises(Exception):
            publish.preflight(client, inputs)

    def test_refuses_when_environment_missing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(environment=None)  # no release environment
            with self.assertRaises(Exception):
                publish.publish_release_from_dir(client, out, self._inputs())
            self.assertNotIn("create_release", client.calls)

    def test_refuses_when_environment_has_no_required_reviewers(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            unprotected = {"name": "release", "protection_rules": [{"id": 9, "type": "wait_timer", "wait_timer": 5}]}
            client = _ready_client(environment=unprotected)
            with self.assertRaises(Exception):
                publish.publish_release_from_dir(client, out, self._inputs())
            self.assertNotIn("create_release", client.calls)


class EnvironmentValidationTests(unittest.TestCase):
    def test_missing_environment_rejected(self):
        client = _ready_client(environment=None)
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_environment_protected(client, environment="release")

    def test_environment_without_required_reviewers_rejected(self):
        unprotected = {"name": "release", "protection_rules": [{"id": 9, "type": "wait_timer", "wait_timer": 5}]}
        client = _ready_client(environment=unprotected)
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_environment_protected(client, environment="release")

    def test_protected_environment_accepted(self):
        client = _ready_client(environment=_protected_environment())
        validate_mod.validate_environment_protected(client, environment="release")  # no raise

    def test_malformed_protection_rules_rejected(self):
        client = _ready_client(environment={"name": "release", "protection_rules": "nope"})
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_environment_protected(client, environment="release")


if __name__ == "__main__":
    unittest.main()
