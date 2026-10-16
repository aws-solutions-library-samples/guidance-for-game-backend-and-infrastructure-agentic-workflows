"""Settings reporting, post-publish read-back, and upload-host contracts.

These cover the publication guarantees that depend on repository settings and
live GitHub behavior:

* the ``release`` object's ``immutable`` flag is confirmed after publish, so a
  release published without GitHub's immutable-releases setting fails the run
  with a remedy (the setting cannot be read ahead of time with the workflow
  token);
* the four published assets are confirmed by size and, when GitHub reports it,
  digest, and the tag ref resolves through this run's annotated tag object to
  the released commit;
* a best-effort, non-failing settings report notes whether a ``v*`` tag ruleset
  exists;
* the GitHub client refuses to attach the bearer token to an asset upload whose
  host is not the configured uploads origin.
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
from release.github_client import GitHubError  # noqa: E402

COMMIT = "a" * 40


def _write_artifact_set(out_dir: Path, *, tag="v0.1.0", commit=COMMIT) -> None:
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


def _ready_client(**kw):
    client = RecordingClient()
    client.main_head = COMMIT
    client.environments = {"release": _protected_environment()}
    for key, value in kw.items():
        setattr(client, key, value)
    return client


def _metadata(tag="v0.1.0"):
    return publish.PublishMetadata(
        tag=tag,
        commit=COMMIT,
        release_name=tag,
        notes="# notes\n## Defaults\n",
        tagger_name="Release Bot",
        tagger_email="release@example.com",
        commit_date="2024-01-02T03:04:05Z",
    )


class PostPublishReadBackTests(unittest.TestCase):
    def test_happy_path_confirms_immutable_release(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()
            result = publish.publish_release_from_dir(client, out, _metadata())
            self.assertTrue(result.published)
            # The read-back ran: it read the release and listed its assets.
            self.assertIn(f"get_release:{result.release_id}", client.calls)
            self.assertIn(f"list_release_assets:{result.release_id}", client.calls)

    def test_non_immutable_release_fails_the_run(self):
        # A release published without GitHub's immutable-releases setting reads
        # back immutable=false; the read-back must fail the run with a remedy.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(published_immutable=False)
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.publish_release_from_dir(client, out, _metadata())
            self.assertIn("immutable", str(ctx.exception).lower())

    def test_missing_asset_fails_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            original = client.upload_release_asset

            def drop_one(upload_url, *, name, data, content_type):
                if name == "SHA256SUMS":
                    return {"name": name, "state": "uploaded"}  # recorded nowhere
                return original(upload_url, name=name, data=data, content_type=content_type)

            client.upload_release_asset = drop_one  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError):
                publish.publish_release_from_dir(client, out, _metadata())

    def test_asset_size_mismatch_fails_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            original = client.upload_release_asset

            def wrong_size(upload_url, *, name, data, content_type):
                res = original(upload_url, name=name, data=data, content_type=content_type)
                # Corrupt the recorded size for one asset.
                for rid, assets in client.release_assets.items():
                    for asset in assets:
                        if asset["name"] == name == "backend-sbom.spdx.json":
                            asset["size"] = asset["size"] + 1
                return res

            client.upload_release_asset = wrong_size  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError):
                publish.publish_release_from_dir(client, out, _metadata())

    def test_asset_digest_mismatch_fails_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            original = client.upload_release_asset

            def wrong_digest(upload_url, *, name, data, content_type):
                res = original(upload_url, name=name, data=data, content_type=content_type)
                for rid, assets in client.release_assets.items():
                    for asset in assets:
                        if asset["name"] == name == "frontend-sbom.spdx.json":
                            asset["digest"] = "sha256:" + ("0" * 64)
                return res

            client.upload_release_asset = wrong_digest  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError):
                publish.publish_release_from_dir(client, out, _metadata())

    def test_tag_object_pointing_at_wrong_commit_fails_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            # The annotated tag object the run creates resolves to a different
            # commit than the released one.
            def wrong_tag_object(*, tag, message, commit, tagger_name, tagger_email, date):
                sha = "t" * 40
                client.tag_objects[sha] = {"sha": sha, "object": {"sha": "b" * 40, "type": "commit"}}
                client.calls.append("create_tag_object")
                return sha

            client.create_tag_object = wrong_tag_object  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.publish_release_from_dir(client, out, _metadata())
            self.assertIn("commit", str(ctx.exception).lower())

    def test_read_back_failure_leaves_published_release_intact(self):
        # A read-back failure must NOT delete the published release: cleanup
        # sees a published (non-draft) release and leaves it intact.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(published_immutable=False)
            with self.assertRaises(validate_mod.ValidationError):
                publish.publish_release_from_dir(client, out, _metadata())
            # The release is still present and published.
            self.assertTrue(any(not r.get("draft", True) for r in client.releases))
            self.assertNotIn("delete_release", client.calls)


class SettingsReportTests(unittest.TestCase):
    def test_immutable_setting_is_reported_unverifiable(self):
        client = _ready_client()
        report = validate_mod.report_release_settings(client)
        self.assertEqual(report["immutable_releases"], "unverifiable-pre-run")

    def test_tag_ruleset_present_reported(self):
        client = _ready_client(rulesets=[{"name": "protect v*", "target": "tag", "enforcement": "active"}])
        report = validate_mod.report_release_settings(client)
        self.assertEqual(report["tag_ruleset"], "present")

    def test_tag_ruleset_absent_reported(self):
        client = _ready_client(rulesets=[{"name": "branch stuff", "target": "branch"}])
        report = validate_mod.report_release_settings(client)
        self.assertEqual(report["tag_ruleset"], "absent")

    def test_report_never_raises_on_read_error(self):
        client = _ready_client()

        def boom(*, target=None):
            raise RuntimeError("api down")

        client.list_rulesets = boom  # type: ignore[assignment]
        report = validate_mod.report_release_settings(client)
        self.assertEqual(report["tag_ruleset"], "unreadable")
        self.assertEqual(report["immutable_releases"], "unverifiable-pre-run")


class UploadHostContractTests(unittest.TestCase):
    """The real GitHubClient must not attach the token to an unexpected host."""

    def _client(self):
        # Use the real client (not the fake, whose method is overridden) with a
        # transport that must never be reached for a rejected host.
        # Local modules
        from release.github_client import GitHubClient

        def transport(_request):
            raise AssertionError("transport must not be called for a rejected host")

        return GitHubClient(owner="o", repo="r", token="t", transport=transport)  # noqa: S106 - test token

    def test_rejects_non_uploads_host(self):
        client = self._client()
        with self.assertRaises(GitHubError):
            client.upload_release_asset(
                "https://evil.example.com/releases/1/assets{?name,label}",
                name="x",
                data=b"y",
                content_type="text/plain",
            )

    def test_rejects_http_scheme(self):
        client = self._client()
        with self.assertRaises(GitHubError):
            client.upload_release_asset(
                "http://uploads.github.com/releases/1/assets",
                name="x",
                data=b"y",
                content_type="text/plain",
            )

    def test_accepts_configured_uploads_host(self):
        # A correct host passes the host check and reaches the transport, which
        # returns a 201 for the asset.
        # Local modules
        from release.github_client import GitHubClient, Response

        def transport(request):
            assert request.url.startswith("https://uploads.github.com/")
            return Response(status=201, headers={}, body=b'{"state": "uploaded"}')

        client = GitHubClient(owner="o", repo="r", token="t", transport=transport)  # noqa: S106 - test token
        res = client.upload_release_asset(
            "https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}",
            name="SHA256SUMS",
            data=b"abc",
            content_type="text/plain",
        )
        self.assertEqual(res.get("state"), "uploaded")


if __name__ == "__main__":
    unittest.main()
