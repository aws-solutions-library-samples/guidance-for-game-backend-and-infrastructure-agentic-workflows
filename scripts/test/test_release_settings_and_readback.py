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
from _release_fakes import RecordingClient, apply_release_settings, covering_tag_ruleset  # noqa: E402

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


def _ready_client(**kw):
    client = RecordingClient()
    client.main_head = COMMIT
    apply_release_settings(client)
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


def _artifact_inputs(tag="v0.1.0"):
    """Build a PublishInputs with the real four-artifact set for read-back tests."""
    manifest = (
        '{\n  "attached_files": [\n    "SHA256SUMS",\n'
        '    "backend-sbom.spdx.json",\n    "frontend-sbom.spdx.json",\n'
        '    "release-manifest.json"\n  ],\n'
        f'  "commit": "{COMMIT}",\n'
        '  "schema_version": "gbaw.release-manifest.v1",\n'
        f'  "tag": "{tag}"\n}}\n'
    ).encode("utf-8")
    arts, sums = artifacts_mod.assemble_artifacts(
        manifest_bytes=manifest,
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
        tagger_name="n",
        tagger_email="e",
        commit_date="2024-01-02T03:04:05Z",
    )


def _record_assets(client, release_id, inputs):
    """Record the four assets (correct name/size/digest) against ``release_id``."""
    # Standard library
    import hashlib

    client.release_assets[release_id] = [
        {"name": a.name, "size": len(a.data), "digest": f"sha256:{hashlib.sha256(a.data).hexdigest()}"}
        for a in inputs.artifacts
    ]


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

    def test_tag_ruleset_present_reported_when_v_tags_covered(self):
        # An active tag ruleset whose ref conditions cover refs/tags/v* is
        # reported present (coverage is confirmed by reading the ruleset, not
        # merely by a tag ruleset existing).
        client = RecordingClient()
        summary, detail = covering_tag_ruleset()
        client.rulesets = [summary]
        client.ruleset_details = {summary["id"]: detail}
        report = validate_mod.report_release_settings(client)
        self.assertEqual(report["tag_ruleset"], "present")

    def test_tag_ruleset_absent_when_conditions_do_not_cover_v_tags(self):
        # A tag ruleset that only covers a different tag pattern is reported
        # absent, so the maintainer is prompted to add v* coverage.
        client = RecordingClient()
        summary = {"id": 7, "name": "untagged", "target": "tag", "enforcement": "active"}
        detail = {
            "id": 7,
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/tags/untagged-*"], "exclude": []}},
        }
        client.rulesets = [summary]
        client.ruleset_details = {7: detail}
        report = validate_mod.report_release_settings(client)
        self.assertEqual(report["tag_ruleset"], "absent")

    def test_tag_ruleset_absent_reported(self):
        client = RecordingClient()
        client.rulesets = [{"id": 3, "name": "branch stuff", "target": "branch"}]
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


class TagRulesetGateTests(unittest.TestCase):
    """The non-dry publish must fail closed unless an active tag ruleset covers
    refs/tags/v*; a dry-run report observes coverage without failing."""

    def test_gate_passes_when_v_tags_covered(self):
        client = RecordingClient()
        apply_release_settings(client)
        # Does not raise.
        validate_mod.validate_tag_ruleset_present(client)

    def test_gate_fails_when_no_tag_ruleset(self):
        client = RecordingClient()
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_tag_ruleset_present(client)

    def test_gate_fails_when_ruleset_does_not_cover_v_tags(self):
        client = RecordingClient()
        summary = {"id": 7, "name": "untagged", "target": "tag", "enforcement": "active"}
        detail = {
            "id": 7,
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/tags/untagged-*"], "exclude": []}},
        }
        client.rulesets = [summary]
        client.ruleset_details = {7: detail}
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_tag_ruleset_present(client)

    def test_tilde_all_include_covers_v_tags(self):
        client = RecordingClient()
        summary = {"id": 9, "name": "all", "target": "tag", "enforcement": "active"}
        detail = {
            "id": 9,
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
        }
        client.rulesets = [summary]
        client.ruleset_details = {9: detail}
        self.assertTrue(validate_mod.tag_ruleset_covers_v_tags(client))

    def test_exclude_of_v_tags_defeats_coverage(self):
        client = RecordingClient()
        summary = {"id": 10, "name": "all-but-v", "target": "tag", "enforcement": "active"}
        detail = {
            "id": 10,
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["refs/tags/v*"]}},
        }
        client.rulesets = [summary]
        client.ruleset_details = {10: detail}
        self.assertFalse(validate_mod.tag_ruleset_covers_v_tags(client))

    def test_inactive_ruleset_does_not_cover(self):
        client = RecordingClient()
        summary = {"id": 11, "name": "disabled", "target": "tag", "enforcement": "disabled"}
        detail = {
            "id": 11,
            "target": "tag",
            "enforcement": "disabled",
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        }
        client.rulesets = [summary]
        client.ruleset_details = {11: detail}
        self.assertFalse(validate_mod.tag_ruleset_covers_v_tags(client))


class EnvironmentPolicyGateTests(unittest.TestCase):
    """The non-dry publish must fail closed unless the environment also prevents
    self-review and limits deployments to a main-only branch policy."""

    def _client(self):
        client = RecordingClient()
        apply_release_settings(client)
        return client

    def test_full_policy_passes(self):
        validate_mod.validate_environment_policy(self._client())

    def test_missing_prevent_self_review_fails(self):
        client = self._client()
        client.environments["release"]["protection_rules"][0]["prevent_self_review"] = False
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_environment_policy(client)
        self.assertIn("self-review", str(ctx.exception).lower())

    def test_branch_policy_not_limited_to_main_fails(self):
        client = self._client()
        client.deployment_branch_policies["release"] = [{"id": 1, "name": "main"}, {"id": 2, "name": "release/*"}]
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_environment_policy(client)
        self.assertIn("branch policy", str(ctx.exception).lower())

    def test_missing_custom_branch_policy_fails(self):
        client = self._client()
        client.environments["release"]["deployment_branch_policy"] = {
            "protected_branches": True,
            "custom_branch_policies": False,
        }
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_environment_policy(client)


class ReadBackRetryTests(unittest.TestCase):
    """The read-back GETs retry a transient miss but never delete a published
    release once the publish PATCH returned success."""

    def test_transient_get_release_miss_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            calls = {"n": 0}
            real_get_release = client.get_release

            def flaky_get_release(release_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("eventual consistency: 404")
                return real_get_release(release_id)

            client.get_release = flaky_get_release  # type: ignore[assignment]
            # Make the retry sleep a no-op so the test does not wait.
            publish._sleep = lambda _s: None  # type: ignore[assignment]
            result = publish.publish_release_from_dir(client, out, _metadata())
            self.assertTrue(result.published)
            self.assertGreaterEqual(calls["n"], 2)

    def test_no_delete_after_successful_publish_even_if_read_back_fails(self):
        # published is True after the PATCH; a read-back failure must not delete
        # the release or the tag ref (use result.published, not a live re-read).
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(published_immutable=False)
            publish._sleep = lambda _s: None  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError):
                publish.publish_release_from_dir(client, out, _metadata())
            self.assertNotIn("delete_release", client.calls)
            self.assertNotIn("delete_ref", client.calls)
            self.assertTrue(any(not r.get("draft", True) for r in client.releases))


class PreTagDraftAssetCheckTests(unittest.TestCase):
    """A draft asset altered before the tag write is caught pre-tag, so the tag
    name is not burned and the draft can be cleaned up."""

    def test_altered_draft_asset_fails_before_tag_creation(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            original = client.upload_release_asset

            def corrupt_size(upload_url, *, name, data, content_type):
                res = original(upload_url, name=name, data=data, content_type=content_type)
                for _rid, assets in client.release_assets.items():
                    for asset in assets:
                        if asset["name"] == name == "backend-sbom.spdx.json":
                            asset["size"] = asset["size"] + 7
                return res

            client.upload_release_asset = corrupt_size  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.publish_release_from_dir(client, out, _metadata())
            self.assertIn("pre-tag", str(ctx.exception).lower())
            # The tag object was never created, so the tag name is not burned.
            self.assertNotIn("create_tag_object", client.calls)


class DigestOptionalWhenAbsentTests(unittest.TestCase):
    """When GitHub does not report a digest, the read-back falls back to the
    size check rather than failing on a null digest."""

    def test_absent_digest_is_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            original = client.upload_release_asset

            def no_digest(upload_url, *, name, data, content_type):
                res = original(upload_url, name=name, data=data, content_type=content_type)
                for _rid, assets in client.release_assets.items():
                    for asset in assets:
                        if asset["name"] == name:
                            asset.pop("digest", None)  # GitHub reports no digest
                return res

            client.upload_release_asset = no_digest  # type: ignore[assignment]
            result = publish.publish_release_from_dir(client, out, _metadata())
            self.assertTrue(result.published)


class RedirectHostContractTests(unittest.TestCase):
    """An authenticated request must not follow a redirect to a different host."""

    def test_cross_host_redirect_is_refused(self):
        # Standard library
        import urllib.request

        # Local modules
        from release.github_client import _NoCrossHostRedirect

        handler = _NoCrossHostRedirect()
        req = urllib.request.Request("https://api.github.com/repos/o/r/releases/1")
        with self.assertRaises(Exception):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/elsewhere")

    def test_same_host_redirect_is_allowed(self):
        # Standard library
        import urllib.request

        # Local modules
        from release.github_client import _NoCrossHostRedirect

        handler = _NoCrossHostRedirect()
        req = urllib.request.Request("https://api.github.com/repos/o/r/releases/1")
        result = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/repos/o/r/releases/2")
        self.assertIsNotNone(result)


class ReadMethodTransportUrlTests(unittest.TestCase):
    """The real client's read methods must hit the documented URLs, so a wrong
    path in get_release / list_release_assets / get_tag_object / rulesets fails
    (the fakes override these methods, so only a transport-level test catches a
    bad URL)."""

    def _client_recording(self, body=b"{}"):
        # Local modules
        from release.github_client import GitHubClient, Response

        seen = {}

        def transport(request):
            seen["url"] = request.url
            seen["method"] = request.method
            return Response(status=200, headers={}, body=body)

        client = GitHubClient(owner="o", repo="r", token="t", transport=transport)  # noqa: S106 - test token
        return client, seen

    def test_get_release_url(self):
        client, seen = self._client_recording(body=b'{"id": 5}')
        client.get_release(5)
        self.assertEqual(seen["url"], "https://api.github.com/repos/o/r/releases/5")

    def test_list_release_assets_url(self):
        client, seen = self._client_recording(body=b"[]")
        client.list_release_assets(5)
        self.assertTrue(seen["url"].startswith("https://api.github.com/repos/o/r/releases/5/assets"))

    def test_get_tag_object_url(self):
        client, seen = self._client_recording(body=b'{"sha": "x"}')
        client.get_tag_object("deadbeef")
        self.assertEqual(seen["url"], "https://api.github.com/repos/o/r/git/tags/deadbeef")

    def test_list_rulesets_url_uses_targets_tag(self):
        client, seen = self._client_recording(body=b"[]")
        client.list_rulesets(target="tag")
        self.assertIn("/repos/o/r/rulesets?targets=tag", seen["url"])

    def test_get_ruleset_url(self):
        client, seen = self._client_recording(body=b'{"id": 42}')
        client.get_ruleset(42)
        self.assertEqual(seen["url"], "https://api.github.com/repos/o/r/rulesets/42")

    def test_list_deployment_branch_policies_url(self):
        client, seen = self._client_recording(body=b'{"branch_policies": []}')
        client.list_deployment_branch_policies("release")
        self.assertEqual(
            seen["url"], "https://api.github.com/repos/o/r/environments/release/deployment-branch-policies"
        )


class ReadBackCheckMutationTests(unittest.TestCase):
    """Each read-back check must have a test that fails if the check is dropped:
    still-a-draft, tag_name, ref==this run's tag object, lightweight-ref, and
    absent-ref."""

    def _args(self, client, release_id, inputs, created_tag_sha="t" * 40):
        return dict(client=client, release_id=release_id, inputs=inputs, created_tag_sha=created_tag_sha)

    def _publish_once(self, **overrides):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(**overrides)
            publish._sleep = lambda _s: None  # type: ignore[assignment]
            publish.publish_release_from_dir(client, out, _metadata())
            return client

    def test_still_a_draft_check_rejects_a_draft_readback(self):
        # If the release reads back as still a draft, the read-back must fail.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            real_update = client.update_release

            def no_publish(release_id, *, draft=None, prerelease=None, make_latest=None):
                # Record the call but leave the release a draft.
                client.calls.append("update_release")
                return {"id": release_id, "draft": True}

            client.update_release = no_publish  # type: ignore[assignment]
            publish._sleep = lambda _s: None  # type: ignore[assignment]
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.publish_release_from_dir(client, out, _metadata())
            self.assertIn("draft", str(ctx.exception).lower())

    def test_tag_name_mismatch_fails(self):
        inputs = publish.PublishInputs(
            tag="v9.9.9",
            commit=COMMIT,
            release_name="v9.9.9",
            notes="x",
            artifacts=[],
            sha256sums=b"",
            tagger_name="n",
            tagger_email="e",
            commit_date="2024-01-02T03:04:05Z",
        )
        client = _ready_client()
        client.releases.append({"id": 500, "tag_name": "v0.0.0", "draft": False})
        client.published_immutable = True
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            publish.verify_published_release(client, release_id=500, inputs=inputs, created_tag_sha=None)
        self.assertIn("tag_name", str(ctx.exception).lower())

    def test_ref_not_this_runs_tag_object_fails(self):
        inputs = _artifact_inputs()
        client = _ready_client()
        client.releases.append({"id": 501, "tag_name": "v0.1.0", "draft": False})
        client.published_immutable = True
        _record_assets(client, 501, inputs)
        client.existing_refs["tags/v0.1.0"] = "OTHERSHA"  # ref points elsewhere
        publish._sleep = lambda _s: None  # type: ignore[assignment]
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            publish.verify_published_release(client, release_id=501, inputs=inputs, created_tag_sha="t" * 40)
        self.assertIn("annotated tag object", str(ctx.exception).lower())

    def test_lightweight_ref_rejected(self):
        inputs = _artifact_inputs()
        client = _ready_client()
        client.releases.append({"id": 502, "tag_name": "v0.1.0", "draft": False})
        client.published_immutable = True
        _record_assets(client, 502, inputs)
        # A lightweight ref points directly at a commit object.
        client.get_ref = lambda ref: {  # type: ignore[assignment]
            "ref": f"refs/{ref}",
            "object": {"sha": "c" * 40, "type": "commit"},
        }
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            publish.verify_published_release(client, release_id=502, inputs=inputs, created_tag_sha=None)
        self.assertIn("not an annotated tag", str(ctx.exception).lower())

    def test_absent_ref_fails(self):
        inputs = _artifact_inputs()
        client = _ready_client()
        client.releases.append({"id": 503, "tag_name": "v0.1.0", "draft": False})
        client.published_immutable = True
        _record_assets(client, 503, inputs)
        # No ref recorded -> get_ref returns None.
        publish._sleep = lambda _s: None  # type: ignore[assignment]
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            publish.verify_published_release(client, release_id=503, inputs=inputs, created_tag_sha=None)
        self.assertIn("tag ref is absent", str(ctx.exception).lower())


class CliVerifyEnvironmentReportTests(unittest.TestCase):
    """cmd_verify_environment must print the settings report, so dropping the
    report call (or the ruleset read) is caught."""

    def test_prints_tag_ruleset_line(self):
        # Standard library
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        # Local modules
        from release import cli

        client = _ready_client()
        buf = io.StringIO()
        with (
            mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
            redirect_stdout(buf),
        ):
            rc = cli.cmd_verify_environment(mock.MagicMock())
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("v* tag ruleset=present", out)
        self.assertIn("immutable-releases=", out)

    def test_reports_absent_when_no_covering_ruleset(self):
        # Standard library
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        # Local modules
        from release import cli

        client = _ready_client()
        client.rulesets = []
        client.ruleset_details = {}
        buf = io.StringIO()
        with (
            mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
            redirect_stdout(buf),
        ):
            cli.cmd_verify_environment(mock.MagicMock())
        self.assertIn("v* tag ruleset=absent", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
