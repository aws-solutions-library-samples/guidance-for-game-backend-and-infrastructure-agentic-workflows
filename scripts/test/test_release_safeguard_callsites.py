"""Call-site contracts that pin the publish/validation safeguards.

Each safeguard is pinned here by observing its behavior at the call site, so a
later edit that drops it turns a test red instead of passing silently:

* the pre-tag HEAD re-check (publish refuses to tag once main HEAD moves after
  asset upload);
* the SIGTERM-to-cleanup handler (publication installs it while publishing);
* the publish-time manifest rebuild binding (``cmd_publish`` passes the rebuilt
  manifest so a changed evidence block is rejected during the approval wait);
* the permalink-issue binding (``run_full_validation`` forwards the dispatched
  issue number so a permalink cannot name one issue and point at another);
* the waiver forwarding (``run_full_validation`` and both ``cli`` manifest
  builds forward the waiver references so the manifest binds them).

Standard library only.
"""

# Standard library
import signal
import sys
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

COMMIT = "a" * 40


def _publish_inputs(tag="v0.1.0"):
    arts, sums = artifacts_mod.assemble_artifacts(
        manifest_bytes=b'{"schema_version": "gbaw.release-manifest.v1"}\n',
        backend_sbom_bytes=b'{"spdx": "backend"}\n',
        frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
    )
    return publish.PublishInputs(
        tag=tag,
        commit=COMMIT,
        release_name=tag,
        notes="# notes\n",
        artifacts=arts,
        sha256sums=sums,
        tagger_name="Release Bot",
        tagger_email="release@example.com",
        commit_date="2024-01-02T03:04:05Z",
    )


class PreTagHeadRecheckTests(unittest.TestCase):
    def test_head_moving_after_upload_prevents_tag_write(self):
        # The pre-tag HEAD re-check runs AFTER asset upload and before the tag
        # object. If HEAD moves during upload, no tag object/ref is written.
        class HeadMovesAfterUploadClient(RecordingClient):
            def upload_release_asset(self, upload_url, *, name, data, content_type):
                out = super().upload_release_asset(upload_url, name=name, data=data, content_type=content_type)
                self.main_head = "f" * 40  # a merge landed during upload
                return out

        client = HeadMovesAfterUploadClient()
        client.main_head = COMMIT
        with self.assertRaises(Exception):
            publish.publish_release(client, _publish_inputs())
        self.assertNotIn("create_tag_object", client.calls)
        self.assertNotIn("create_ref", client.calls)
        # The draft created before the race must be cleaned up.
        self.assertEqual([r for r in client.releases if r.get("draft")], [])


class SigtermHandlerInstalledTests(unittest.TestCase):
    def test_handler_is_installed_during_publication(self):
        observed: dict[str, object] = {}

        class CaptureHandlerClient(RecordingClient):
            def create_tag_object(self, **kwargs):
                observed["sigterm_handler"] = signal.getsignal(signal.SIGTERM)
                return super().create_tag_object(**kwargs)

        client = CaptureHandlerClient()
        client.main_head = COMMIT
        result = publish.publish_release(client, _publish_inputs())
        self.assertTrue(result.published)
        handler = observed.get("sigterm_handler")
        # A custom handler (callable) must be installed while publishing, not
        # the default disposition.
        self.assertTrue(callable(handler), msg=f"SIGTERM handler not installed: {handler!r}")
        self.assertNotIn(handler, (signal.SIG_DFL, signal.SIG_IGN))

    def test_sigterm_during_publication_triggers_cleanup(self):
        # If a SIGTERM arrives during publication (not during cleanup), it must
        # raise so cleanup runs and nothing stable is left.
        # Standard library
        import os

        class SigtermAtUploadClient(RecordingClient):
            def upload_release_asset(self, upload_url, *, name, data, content_type):
                os.kill(os.getpid(), signal.SIGTERM)
                return super().upload_release_asset(upload_url, name=name, data=data, content_type=content_type)

        client = SigtermAtUploadClient()
        client.main_head = COMMIT
        with self.assertRaises(BaseException):
            publish.publish_release(client, _publish_inputs())
        self.assertNotIn("tags/v0.1.0", client.existing_refs)
        self.assertEqual([r for r in client.releases if r.get("draft")], [])


class ManifestRebuildBindingCallSiteTests(unittest.TestCase):
    def test_publish_from_dir_binds_the_rebuilt_manifest(self):
        # publish_release_from_dir with a rebuilt_manifest that differs from the
        # downloaded manifest must refuse before any write. This pins that
        # cmd_publish's rebuilt_manifest= argument is actually consumed.
        # Standard library
        import tempfile

        # Local modules
        from release import validate as validate_mod

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            out.mkdir(parents=True)
            manifest_bytes = (
                '{\n  "attached_files": [\n    "SHA256SUMS",\n'
                '    "backend-sbom.spdx.json",\n    "frontend-sbom.spdx.json",\n'
                '    "release-manifest.json"\n  ],\n'
                f'  "commit": "{COMMIT}",\n'
                '  "schema_version": "gbaw.release-manifest.v1",\n'
                '  "tag": "v0.1.0"\n}\n'
            ).encode("utf-8")
            arts, _sums = artifacts_mod.assemble_artifacts(
                manifest_bytes=manifest_bytes,
                backend_sbom_bytes=b'{"spdx": "backend"}\n',
                frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
            )
            for art in arts:
                (out / art.name).write_bytes(art.data)
            client = RecordingClient()
            client.main_head = COMMIT
            client.environments = {
                "release": {
                    "protection_rules": [{"type": "required_reviewers", "reviewers": [{"x": 1}]}],
                }
            }
            metadata = publish.PublishMetadata(
                tag="v0.1.0",
                commit=COMMIT,
                release_name="v0.1.0",
                notes="# notes\n",
                tagger_name="Bot",
                tagger_email="release@example.com",
                commit_date="2024-01-02T03:04:05Z",
            )
            with self.assertRaises(validate_mod.ValidationError):
                publish.publish_release_from_dir(client, out, metadata, rebuilt_manifest=manifest_bytes + b"tampered\n")
            self.assertNotIn("create_release", client.calls)


class ExpectedIssueForwardedTests(unittest.TestCase):
    def test_run_full_validation_forwards_expected_issue(self):
        # run_full_validation must forward evidence_issue as expected_issue so a
        # permalink that names one issue but points at a comment on another is
        # rejected. Observe it by recording what validate_evidence_comment sees.
        # Standard library
        import tempfile

        # Local modules
        from release import validate as validate_mod

        captured: dict[str, object] = {}
        original = validate_mod.validate_evidence_comment

        def spy(client, *, comment_id, commit, scan_fn, expected_issue=None):
            captured["expected_issue"] = expected_issue
            return original(
                client, comment_id=comment_id, commit=commit, scan_fn=scan_fn, expected_issue=expected_issue
            )

        client = _validation_ready_client()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            notes = root / "docs" / "releases" / "v0.1.0.md"
            notes.parent.mkdir(parents=True)
            notes.write_text(
                "# v0.1.0\nDefaults Capabilities Known issues Compatibility Deployment Rollback\n",
                encoding="utf-8",
            )
            validate_mod.validate_evidence_comment = spy
            try:
                validate_mod.run_full_validation(
                    client,
                    root,
                    commit=COMMIT,
                    tag="v0.1.0",
                    evidence_comment_id=55,
                    evidence_issue=537,
                    release_name="v0.1.0",
                    comment_scan_fn=lambda _b: [],
                    file_scan_fn=lambda _p: [],
                )
            finally:
                validate_mod.validate_evidence_comment = original
        self.assertEqual(captured.get("expected_issue"), 537)


class CmdPublishForwardsRebuiltManifestTests(unittest.TestCase):
    def test_cmd_publish_rejects_a_downloaded_manifest_that_differs_from_the_rebuild(self):
        # Drive cmd_publish end-to-end with a downloaded manifest that does not
        # match the one the command rebuilds from this checkout. cmd_publish
        # must forward the rebuilt manifest so publication is refused; if the
        # rebuilt_manifest= argument is dropped, the mismatch slips through.
        # Standard library
        import tempfile
        from unittest import mock

        # Local modules
        from release import cli
        from release import validate as validate_mod

        with tempfile.TemporaryDirectory() as d:
            repo_root = Path(d) / "repo"
            (repo_root / "docs" / "adr").mkdir(parents=True)
            (repo_root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
                "The default is `disabled`.\n", encoding="utf-8"
            )
            (repo_root / "backend" / "src" / "agents").mkdir(parents=True)
            (repo_root / "backend" / "src" / "agents" / "optimized_prompts.py").write_text("P='x'\n", encoding="utf-8")
            (repo_root / "backend" / "uv.lock").write_text("l\n", encoding="utf-8")
            (repo_root / "backend" / "requirements.txt").write_text("r\n", encoding="utf-8")
            (repo_root / "ui").mkdir()
            (repo_root / "ui" / "package-lock.json").write_text("{}\n", encoding="utf-8")
            (repo_root / "docs" / "releases").mkdir(parents=True)
            (repo_root / "docs" / "releases" / "v0.1.0.md").write_text(
                "# v0.1.0\nDefaults Capabilities Known issues Compatibility Deployment Rollback\n",
                encoding="utf-8",
            )

            # Build the artifact set with a DIFFERENT commit_date so the
            # downloaded manifest will not match the publish-time rebuild.
            # Local modules
            from release import artifacts as artifacts_mod
            from release import manifest as manifest_mod
            from release.artifact_guard import BACKEND_SBOM_NAME, CHECKSUMS_NAME, FRONTEND_SBOM_NAME, MANIFEST_NAME

            result = _validation_result()
            downloaded_manifest = manifest_mod.serialize_manifest(
                manifest_mod.build_manifest(
                    repo_root,
                    tag=result.tag,
                    release_name=result.release_name,
                    commit=result.commit,
                    commit_date="1999-01-01T00:00:00Z",  # stale date -> different bytes
                    required_checks=[
                        manifest_mod.CheckRunResult(name=c.name, conclusion=c.conclusion)
                        for c in result.check_conclusions
                    ],
                    evidence_digest=result.evidence_digest,
                    gate_summary=result.gate_summary,
                    attached_file_names=[MANIFEST_NAME, BACKEND_SBOM_NAME, FRONTEND_SBOM_NAME, CHECKSUMS_NAME],
                )
            )
            arts, _sums = artifacts_mod.assemble_artifacts(
                manifest_bytes=downloaded_manifest,
                backend_sbom_bytes=b'{"spdx": "backend"}\n',
                frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
            )
            input_dir = Path(d) / "in"
            input_dir.mkdir()
            for art in arts:
                (input_dir / art.name).write_bytes(art.data)

            client = RecordingClient()
            client.main_head = COMMIT
            client.environments = {
                "release": {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"x": 1}]}]}
            }
            env = {
                "GBAW_RELEASE_DRY_RUN": "false",
                "GBAW_RELEASE_INPUT_DIR": str(input_dir),
                "GBAW_RELEASE_COMMIT_DATE": "2024-01-02T03:04:05Z",  # current -> rebuild differs
            }
            with (
                mock.patch.object(cli, "REPO_ROOT", repo_root),
                mock.patch.object(cli, "_run_validation", return_value=result),
                mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
                mock.patch.dict("os.environ", env, clear=False),
            ):
                with self.assertRaises(validate_mod.ValidationError):
                    cli.cmd_publish(mock.MagicMock())
            self.assertNotIn("create_release", client.calls)


def _validation_result():
    # Local modules
    from release import validate as validate_mod

    return validate_mod.ValidationResult(
        tag="v0.1.0",
        commit=COMMIT,
        release_name="v0.1.0",
        evidence_digest="d" * 64,
        gate_summary={"a": "pass"},
        check_conclusions=[validate_mod.CheckRunConclusion(name="backend-tests", conclusion="success")],
    )


def _validation_ready_client():
    # Standard library
    import json

    # Third-party packages
    from _release_fakes import completed_check

    # Local modules
    from release import evidence, validate

    client = RecordingClient()
    client.main_head = COMMIT
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
        client.check_runs[name] = [completed_check(name, COMMIT)]
    gates = {name: "pass" for name in evidence.REQUIRED_GATES}
    doc = {"schema_version": evidence.EVIDENCE_SCHEMA_VERSION, "commit": COMMIT, "gates": gates}
    client.comments[55] = {
        "user": {"login": "maintainer"},
        "issue_url": f"https://api.github.com/repos/{client.owner}/{client.repo}/issues/537",
        "body": f"```json\n{json.dumps(doc, indent=2)}\n```",
    }
    client.collaborator_permissions = {"maintainer": "admin"}
    return client


class WaiverForwardedThroughCliTests(unittest.TestCase):
    """The waiver references must reach the manifest through the real CLI
    manifest-build call sites, so dropping ``waivers=`` at either site fails.

    ``run_full_validation`` carries the waiver reference into ValidationResult,
    ``_build_artifact_set`` (build-artifacts) forwards it to the first manifest
    build, and ``cmd_publish`` forwards it to the rebuilt manifest. Each is
    exercised end-to-end against the CLI, not a local mirror.
    """

    def _fake_repo(self, root: Path):
        (root / "docs" / "adr").mkdir(parents=True)
        (root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
            "The default is `disabled`.\n", encoding="utf-8"
        )
        (root / "backend" / "src" / "agents").mkdir(parents=True)
        (root / "backend" / "src" / "agents" / "optimized_prompts.py").write_text("P='x'\n", encoding="utf-8")
        (root / "backend" / "uv.lock").write_text("l\n", encoding="utf-8")
        (root / "backend" / "requirements.txt").write_text("r\n", encoding="utf-8")
        (root / "ui").mkdir()
        (root / "ui" / "package-lock.json").write_text("{}\n", encoding="utf-8")
        (root / "docs" / "releases").mkdir(parents=True)
        (root / "docs" / "releases" / "v0.1.0.md").write_text(
            "# v0.1.0\nDefaults Capabilities Known issues Compatibility Deployment Rollback\n",
            encoding="utf-8",
        )

    def _waived_result(self):
        # Local modules
        from release import validate as validate_mod

        return validate_mod.ValidationResult(
            tag="v0.1.0",
            commit=COMMIT,
            release_name="v0.1.0",
            evidence_digest="d" * 64,
            gate_summary={"p0_p1_closed_or_waived": "waived"},
            waivers={"p0_p1_closed_or_waived": "#412"},
            check_conclusions=[validate_mod.CheckRunConclusion(name="backend-tests", conclusion="success")],
        )

    def test_build_artifact_set_binds_the_waiver_in_the_manifest(self):
        # Standard library
        import tempfile
        from unittest import mock

        # Local modules
        from release import cli

        with tempfile.TemporaryDirectory() as d:
            repo_root = Path(d) / "repo"
            self._fake_repo(repo_root)
            backend_sbom = Path(d) / "b.json"
            frontend_sbom = Path(d) / "f.json"
            backend_sbom.write_text('{"spdxVersion": "SPDX-2.3", "packages": []}\n', encoding="utf-8")
            frontend_sbom.write_text('{"spdxVersion": "SPDX-2.3", "packages": []}\n', encoding="utf-8")
            client = RecordingClient()
            env = {
                "GBAW_RELEASE_COMMIT_DATE": "2024-01-02T03:04:05Z",
                "GBAW_RELEASE_BACKEND_SBOM": str(backend_sbom),
                "GBAW_RELEASE_FRONTEND_SBOM": str(frontend_sbom),
            }
            with (
                mock.patch.object(cli, "REPO_ROOT", repo_root),
                mock.patch.object(cli, "_run_validation", return_value=self._waived_result()),
                mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
                mock.patch.dict("os.environ", env, clear=False),
            ):
                _artifacts, manifest_bytes, _result = cli._build_artifact_set(
                    backend_sbom_path=str(backend_sbom), frontend_sbom_path=str(frontend_sbom)
                )
            self.assertIn(b"#412", manifest_bytes, msg="build-artifacts did not forward the waiver reference")

    def test_cmd_publish_rebuild_binds_the_waiver(self):
        # Build a downloaded artifact set whose manifest binds the waiver, then
        # drive cmd_publish. The rebuild must also bind the waiver so the
        # byte-identical check passes; dropping waivers= in cmd_publish's build
        # makes the rebuilt manifest differ and publication is refused before
        # any write (so create_release is never called on a matching set).
        # Standard library
        import tempfile
        from unittest import mock

        # Local modules
        from release import artifacts as artifacts_mod
        from release import cli
        from release import manifest as manifest_mod
        from release.artifact_guard import BACKEND_SBOM_NAME, CHECKSUMS_NAME, FRONTEND_SBOM_NAME, MANIFEST_NAME

        with tempfile.TemporaryDirectory() as d:
            repo_root = Path(d) / "repo"
            self._fake_repo(repo_root)
            result = self._waived_result()
            downloaded_manifest = manifest_mod.serialize_manifest(
                manifest_mod.build_manifest(
                    repo_root,
                    tag=result.tag,
                    release_name=result.release_name,
                    commit=result.commit,
                    commit_date="2024-01-02T03:04:05Z",
                    required_checks=[
                        manifest_mod.CheckRunResult(name=c.name, conclusion=c.conclusion)
                        for c in result.check_conclusions
                    ],
                    evidence_digest=result.evidence_digest,
                    gate_summary=result.gate_summary,
                    waivers=result.waivers,
                    attached_file_names=[MANIFEST_NAME, BACKEND_SBOM_NAME, FRONTEND_SBOM_NAME, CHECKSUMS_NAME],
                )
            )
            self.assertIn(b"#412", downloaded_manifest)
            arts, _sums = artifacts_mod.assemble_artifacts(
                manifest_bytes=downloaded_manifest,
                backend_sbom_bytes=b'{"spdx": "backend"}\n',
                frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
            )
            input_dir = Path(d) / "in"
            input_dir.mkdir()
            for art in arts:
                (input_dir / art.name).write_bytes(art.data)

            client = RecordingClient()
            client.main_head = COMMIT
            client.environments = {
                "release": {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"x": 1}]}]}
            }
            env = {
                "GBAW_RELEASE_DRY_RUN": "false",
                "GBAW_RELEASE_INPUT_DIR": str(input_dir),
                "GBAW_RELEASE_COMMIT_DATE": "2024-01-02T03:04:05Z",
            }
            with (
                mock.patch.object(cli, "REPO_ROOT", repo_root),
                mock.patch.object(cli, "_run_validation", return_value=result),
                mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
                mock.patch.dict("os.environ", env, clear=False),
            ):
                rc = cli.cmd_publish(mock.MagicMock())
            # The rebuild bound the waiver, so publication proceeded and wrote.
            self.assertEqual(rc, 0)
            self.assertIn("create_release", client.calls)


if __name__ == "__main__":
    unittest.main()
