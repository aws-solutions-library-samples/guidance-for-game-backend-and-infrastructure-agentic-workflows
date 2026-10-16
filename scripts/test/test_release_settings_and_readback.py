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
from unittest import mock

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

    def test_immutable_failure_remedy_states_the_two_constraints(self):
        # The remedy must say the release cannot be made immutable retroactively
        # and the same tag cannot be re-dispatched while the release/ref exist,
        # not an unqualified "re-run". Reverting the text to a bare re-run fails.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client(published_immutable=False)
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.publish_release_from_dir(client, out, _metadata())
            message = str(ctx.exception).lower()
            self.assertIn("retroactively", message)
            self.assertIn("re-dispatched", message)

    def test_mutable_release_with_bad_asset_reports_both_failures(self):
        # verify_published_release runs every check even when immutability
        # fails, so an operator sees the asset mismatch too (not just the first
        # failure). Checking immutable before assets/tag and short-circuiting
        # would drop the asset failure.
        inputs = _artifact_inputs()
        client = _ready_client(published_immutable=False)
        client.releases.append({"id": 600, "tag_name": "v0.1.0", "draft": False})
        _record_assets(client, 600, inputs)
        # Corrupt one recorded asset size so the asset check also fails.
        client.release_assets[600][0]["size"] = client.release_assets[600][0]["size"] + 3
        client.existing_refs["tags/v0.1.0"] = "t" * 40
        with mock.patch.object(publish, "_sleep", lambda _s: None):
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.verify_published_release(client, release_id=600, inputs=inputs, created_tag_sha="t" * 40)
        message = str(ctx.exception).lower()
        self.assertIn("immutable", message)
        self.assertIn("does not equal the published", message)

    def test_two_bad_assets_are_both_reported(self):
        # The asset check collects every per-asset mismatch, not just the first,
        # so an operator sees all the bad assets at once. Two corrupted assets
        # must both appear in the single raised error.
        inputs = _artifact_inputs()
        client = _ready_client()
        client.releases.append({"id": 610, "tag_name": "v0.1.0", "draft": False})
        _record_assets(client, 610, inputs)
        client.existing_refs["tags/v0.1.0"] = "t" * 40
        # Corrupt the sizes of two different assets.
        client.release_assets[610][0]["size"] = client.release_assets[610][0]["size"] + 1
        client.release_assets[610][1]["size"] = client.release_assets[610][1]["size"] + 2
        bad_names = [client.release_assets[610][0]["name"], client.release_assets[610][1]["name"]]
        with mock.patch.object(publish, "_sleep", lambda _s: None):
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.verify_published_release(client, release_id=610, inputs=inputs, created_tag_sha="t" * 40)
        message = str(ctx.exception)
        for name in bad_names:
            self.assertIn(name, message, f"{name} should be named among the failures")

    def test_persistent_asset_read_error_becomes_a_failure_entry(self):
        # If the asset-list read keeps failing (for example a persistent 502),
        # the read error must become a failure ENTRY rather than escaping and
        # discarding the failures already found. The not-immutable failure and
        # the tag check must still be reported, so the operator sees the full
        # picture, not a bare transport error.
        # Local modules
        from release.github_client import GitHubError

        inputs = _artifact_inputs()
        client = _ready_client(published_immutable=False)
        client.releases.append({"id": 620, "tag_name": "v0.1.0", "draft": False})
        # The tag ref resolves correctly, so only the asset read and the
        # immutability are wrong.
        client.existing_refs["tags/v0.1.0"] = "t" * 40
        client.tag_objects["t" * 40] = {"sha": "t" * 40, "object": {"sha": COMMIT, "type": "tag"}}

        def boom(release_id):
            raise GitHubError(502, "list-release-assets")

        client.list_release_assets = boom  # type: ignore[assignment]
        with mock.patch.object(publish, "_sleep", lambda _s: None):
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.verify_published_release(client, release_id=620, inputs=inputs, created_tag_sha="t" * 40)
        message = str(ctx.exception).lower()
        # The not-immutable failure survived (was not discarded by the raise).
        self.assertIn("immutable", message)
        # The asset read error is reported as a failure entry.
        self.assertIn("502", message)

    def test_persistent_release_read_error_becomes_a_failure_entry(self):
        # If the release read itself keeps failing (for example a persistent
        # 503), it must become a failure ENTRY rather than escaping as a bare
        # GitHubError and skipping the asset and tag checks. The asset and tag
        # checks must still run and report their own failures, so the operator
        # sees the full picture rather than only the transport error.
        # Local modules
        from release.github_client import GitHubError

        inputs = _artifact_inputs()
        client = _ready_client()
        client.releases.append({"id": 625, "tag_name": "v0.1.0", "draft": False})
        # The tag ref is absent (a tag check failure) and the assets are missing
        # (an asset check failure), so both downstream checks have something to
        # report once the release read is collected instead of escaping.

        def boom(release_id):
            raise GitHubError(503, "get-release")

        client.get_release = boom  # type: ignore[assignment]
        with mock.patch.object(publish, "_sleep", lambda _s: None):
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.verify_published_release(client, release_id=625, inputs=inputs, created_tag_sha="t" * 40)
        message = str(ctx.exception).lower()
        # The release read error is a failure entry, not a bare escape.
        self.assertIn("503", message)
        # The asset and tag checks still ran and reported their own failures.
        # Pinning both the asset message and the tag message proves the asset
        # check is not skipped after a failed release read (the release entry
        # alone would satisfy a bare "post-publish read-back" check).
        self.assertIn("attached assets", message)
        self.assertIn("tag ref", message)

    def test_missing_asset_fails_read_back(self):
        # If one of the four assets is never recorded on the published release
        # (for example an upload that reported success but attached nothing),
        # the pre-tag draft asset check fails the run before the tag is created,
        # while the draft is still removable and the tag name is unburned.
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
            with self.assertRaises(validate_mod.ValidationError) as ctx:
                publish.publish_release_from_dir(client, out, _metadata())
        message = str(ctx.exception).lower()
        # The failure is the pre-tag draft asset check, named as such, and it
        # fires before the tag ref is created.
        self.assertIn("attached assets", message)
        self.assertNotIn("create_ref", client.calls)

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
        report = validate_mod.report_release_settings(client, tag="v0.1.0")
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
            "rules": [{"type": "deletion"}],
        }
        client.rulesets = [summary]
        client.ruleset_details = {7: detail}
        report = validate_mod.report_release_settings(client, tag="v0.1.0")
        self.assertEqual(report["tag_ruleset"], "absent")

    def test_tag_ruleset_absent_reported(self):
        client = RecordingClient()
        client.rulesets = [{"id": 3, "name": "branch stuff", "target": "branch"}]
        report = validate_mod.report_release_settings(client, tag="v0.1.0")
        self.assertEqual(report["tag_ruleset"], "absent")

    def test_tag_ruleset_would_block_reported_for_unbypassable_creation(self):
        # A covering ruleset with a creation rule the token cannot bypass would
        # be refused by the publish gate before the draft. The dry-run report
        # surfaces that as "would-block" (not "present"), so the dry run warns
        # before approval rather than the refusal arriving only after it.
        client = RecordingClient()
        summary = {"id": 55, "name": "v creation", "target": "tag", "enforcement": "active"}
        detail = {
            "id": 55,
            "target": "tag",
            "enforcement": "active",
            "current_user_can_bypass": "never",
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "creation"}],
        }
        client.rulesets = [summary]
        client.ruleset_details = {55: detail}
        report = validate_mod.report_release_settings(client, tag="v0.1.0")
        self.assertEqual(report["tag_ruleset"], "would-block")

    def test_tag_ruleset_would_block_from_a_second_covering_ruleset(self):
        # An update-only v* ruleset plus a ~ALL deletion ruleset the token
        # cannot bypass: the aggregate would be refused, so the report is
        # "would-block" even though the first covering ruleset alone is fine.
        client = RecordingClient()
        client.rulesets = [
            {"id": 61, "name": "update v*", "target": "tag", "enforcement": "active"},
            {"id": 62, "name": "all deletion", "target": "tag", "enforcement": "active"},
        ]
        client.ruleset_details = {
            61: {
                "id": 61,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                "rules": [{"type": "update"}],
            },
            62: {
                "id": 62,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
                "rules": [{"type": "deletion"}],
            },
        }
        report = validate_mod.report_release_settings(client, tag="v0.1.0")
        self.assertEqual(report["tag_ruleset"], "would-block")

    def test_report_never_raises_on_read_error(self):
        client = _ready_client()

        def boom(*, target=None):
            raise RuntimeError("api down")

        client.list_rulesets = boom  # type: ignore[assignment]
        report = validate_mod.report_release_settings(client, tag="v0.1.0")
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
    refs/tags/<dispatched tag> and restricts who may create/update/delete it; a
    dry-run report observes coverage without failing."""

    def _ruleset(self, include, *, exclude=None, rules=None, ruleset_id=42, enforcement="active"):
        summary = {"id": ruleset_id, "name": "tag rs", "target": "tag", "enforcement": enforcement}
        detail = {
            "id": ruleset_id,
            "target": "tag",
            "enforcement": enforcement,
            "conditions": {"ref_name": {"include": list(include), "exclude": list(exclude or [])}},
            "rules": list(rules if rules is not None else [{"type": "creation"}, {"type": "deletion"}]),
        }
        client = RecordingClient()
        client.rulesets = [summary]
        client.ruleset_details = {ruleset_id: detail}
        return client

    def _covers_with_restriction(self, client, tag):
        # Observe coverage through the gate's own code path: the dispatched ref
        # is covered by a restricting ruleset iff `_covering_tag_rulesets`
        # returns a covering ruleset that carries a creation/update/deletion
        # rule. This is exactly what `validate_tag_ruleset_present` requires
        # before it then checks the per-ruleset bypass values.
        ref = f"refs/tags/{tag}"
        covering = validate_mod._covering_tag_rulesets(client, ref=ref)
        return any(
            validate_mod._ruleset_rule_types(detail) & validate_mod._TAG_RESTRICTING_RULE_TYPES for detail in covering
        )

    def test_gate_passes_when_v_tags_covered(self):
        client = RecordingClient()
        apply_release_settings(client)
        # Does not raise.
        validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")

    def test_gate_fails_when_no_tag_ruleset(self):
        client = RecordingClient()
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
        # The absent-ruleset remedy recommends an `update` rule (which leaves
        # the workflow's own writes alone), not a bare creation/deletion rule
        # that would itself produce a would-block refusal on the next run.
        self.assertIn("'update' rule", str(ctx.exception))

    def test_gate_fails_when_ruleset_does_not_cover_v_tags(self):
        client = self._ruleset(["refs/tags/untagged-*"])
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")

    def test_tilde_all_include_covers_v_tags(self):
        client = self._ruleset(["~ALL"])
        self.assertTrue(self._covers_with_restriction(client, "v0.1.0"))

    def test_exclude_of_v_tags_defeats_coverage(self):
        client = self._ruleset(["~ALL"], exclude=["refs/tags/v*"])
        self.assertFalse(self._covers_with_restriction(client, "v0.1.0"))

    def test_inactive_ruleset_does_not_cover(self):
        client = self._ruleset(["refs/tags/v*"], enforcement="disabled")
        self.assertFalse(self._covers_with_restriction(client, "v0.1.0"))

    def test_covering_ruleset_without_restricting_rule_does_not_count(self):
        # A ruleset that matches the ref but carries only non-restricting rules
        # (for example non_fast_forward + required_deployments) covers the tag
        # without restricting who creates or updates it, so it must not pass.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "non_fast_forward"}, {"type": "required_deployments"}])
        self.assertFalse(self._covers_with_restriction(client, "v0.1.0"))
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")

    def test_update_only_ruleset_passes_without_bypass(self):
        # An `update`-only ruleset restricts moving an existing tag, which the
        # workflow never does, so it passes the gate even when the token is not
        # a bypass actor.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "update"}])
        client.ruleset_details[42]["current_user_can_bypass"] = "never"
        validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")  # does not raise

    def test_creation_rule_without_bypass_fails_closed(self):
        # A `creation` rule only bypass actors can satisfy would refuse the
        # workflow's own tag-ref write. With current_user_can_bypass=never the
        # gate must fail closed before the draft, naming the create/cleanup
        # blockage and the update/bypass remedies.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "creation"}])
        client.ruleset_details[42]["current_user_can_bypass"] = "never"
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
        message = str(ctx.exception).lower()
        self.assertIn("creation", message)
        self.assertIn("bypass", message)

    def test_deletion_rule_without_bypass_fails_closed(self):
        # `deletion` is GitHub's default; it would refuse cleanup's tag-ref
        # delete and leave an orphaned tag. With no bypass the gate fails closed.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "deletion"}])
        client.ruleset_details[42]["current_user_can_bypass"] = "never"
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
        self.assertIn("deletion", str(ctx.exception).lower())

    def test_creation_rule_absent_bypass_field_fails_closed(self):
        # An absent current_user_can_bypass field is treated as "not a bypass
        # actor" (fail closed), since the field is only present when readable.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "creation"}])
        client.ruleset_details[42].pop("current_user_can_bypass", None)
        with self.assertRaises(validate_mod.ValidationError):
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")

    def test_creation_rule_with_bypass_passes(self):
        # When the token IS a bypass actor (current_user_can_bypass=always), a
        # creation/deletion rule does not block the workflow's writes, so the
        # gate passes.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "creation"}, {"type": "deletion"}])
        client.ruleset_details[42]["current_user_can_bypass"] = "always"
        validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")  # does not raise

    def test_exempt_bypass_passes(self):
        # `exempt` is a documented current_user_can_bypass value under which
        # GitHub does not run the rules for the actor, so a creation/deletion
        # rule does not block the workflow's writes and the gate passes.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "creation"}, {"type": "deletion"}])
        client.ruleset_details[42]["current_user_can_bypass"] = "exempt"
        validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")  # does not raise

    def test_pull_requests_only_bypass_fails_closed(self):
        # `pull_requests_only` lets the actor bypass only on a pull request; a
        # direct refs/tags/<tag> write is not a pull request, so a creation rule
        # still blocks the workflow's own write and the gate must fail closed.
        client = self._ruleset(["refs/tags/v*"], rules=[{"type": "creation"}])
        client.ruleset_details[42]["current_user_can_bypass"] = "pull_requests_only"
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
        self.assertIn("creation", str(ctx.exception).lower())

    def test_blocking_refusal_names_the_ruleset_and_source(self):
        # The refusal must name the blocking ruleset (name, id, source_type) so a
        # maintainer knows which one to change — a repository ruleset they can
        # edit, versus an inherited organization/enterprise ruleset they may only
        # exclude the release tags from or ask its owner about.
        client = RecordingClient()
        client.rulesets = [{"id": 77, "name": "org tag lock", "target": "tag", "enforcement": "active"}]
        client.ruleset_details = {
            77: {
                "id": 77,
                "name": "org tag lock",
                "source_type": "Organization",
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
                "rules": [{"type": "creation"}],
            }
        }
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
        message = str(ctx.exception)
        self.assertIn("org tag lock", message)
        self.assertIn("77", message)
        self.assertIn("Organization", message)
        # And it points at excluding the release tags or asking the owner, not
        # only at editing the ruleset to an update rule.
        self.assertIn("ask its owner", message.lower())

    def _two_rulesets(self, first, second, *, order=("first", "second")):
        # Build a client carrying two active tag rulesets. ``first`` and
        # ``second`` are (include, rules, bypass) tuples; ``order`` controls the
        # order they are listed in (GitHub does not promise an order, and the
        # gate must behave the same way regardless).
        specs = {"first": (10, first), "second": (20, second)}
        client = RecordingClient()
        summaries = []
        details = {}
        for key in order:
            rid, (include, rules, bypass) = specs[key]
            summaries.append({"id": rid, "name": key, "target": "tag", "enforcement": "active"})
            detail = {
                "id": rid,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": bypass,
                "conditions": {"ref_name": {"include": list(include), "exclude": []}},
                "rules": list(rules),
            }
            details[rid] = detail
        client.rulesets = summaries
        client.ruleset_details = details
        return client

    def test_two_rulesets_update_then_deletion_fails_closed_both_orders(self):
        # GitHub aggregates the rules of EVERY covering ruleset. An update-only
        # v* ruleset plus a ~ALL deletion ruleset means the deletion rule still
        # applies to refs/tags/v0.1.0, so cleanup's tag delete would be refused
        # and leave an orphaned tag. The gate must refuse in BOTH list orders,
        # not depend on which ruleset GitHub lists first.
        update_only = (["refs/tags/v*"], [{"type": "update"}], "never")
        all_deletion = (["~ALL"], [{"type": "deletion"}], "never")
        for order in (("first", "second"), ("second", "first")):
            with self.subTest(order=order):
                client = self._two_rulesets(update_only, all_deletion, order=order)
                with self.assertRaises(validate_mod.ValidationError) as ctx:
                    validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
                self.assertIn("deletion", str(ctx.exception).lower())

    def test_two_rulesets_update_then_creation_fails_closed_both_orders(self):
        # An update-only v* ruleset plus a second covering creation ruleset:
        # the creation rule would refuse the workflow's own tag-ref write, so
        # the gate must refuse regardless of list order.
        update_only = (["refs/tags/v*"], [{"type": "update"}], "never")
        creation = (["refs/tags/v*"], [{"type": "creation"}], "never")
        for order in (("first", "second"), ("second", "first")):
            with self.subTest(order=order):
                client = self._two_rulesets(update_only, creation, order=order)
                with self.assertRaises(validate_mod.ValidationError) as ctx:
                    validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
                self.assertIn("creation", str(ctx.exception).lower())

    def test_two_update_only_rulesets_pass(self):
        # Two covering update-only rulesets carry no write-blocking rule, so the
        # aggregate leaves the workflow's create and cleanup writes alone and the
        # gate passes in either order.
        a = (["refs/tags/v*"], [{"type": "update"}], "never")
        b = (["~ALL"], [{"type": "update"}], "never")
        for order in (("first", "second"), ("second", "first")):
            with self.subTest(order=order):
                client = self._two_rulesets(a, b, order=order)
                validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")  # does not raise

    def test_bypass_is_per_ruleset_not_pooled_across_rulesets(self):
        # current_user_can_bypass is reported per ruleset, so a bypass on one
        # covering ruleset must NOT excuse an unbypassable rule in another. A
        # creation rule the token CAN bypass on v* plus a second ~ALL creation
        # rule the token CANNOT bypass still blocks the workflow's own tag-ref
        # create, so the gate must refuse in BOTH list orders. Pooling the
        # bypass value across covering rulesets would wrongly let this pass.
        bypassable = (["refs/tags/v*"], [{"type": "creation"}], "always")
        unbypassable = (["~ALL"], [{"type": "creation"}], "never")
        for order in (("first", "second"), ("second", "first")):
            with self.subTest(order=order):
                client = self._two_rulesets(bypassable, unbypassable, order=order)
                with self.assertRaises(validate_mod.ValidationError) as ctx:
                    validate_mod.validate_tag_ruleset_present(client, tag="v0.1.0")
                self.assertIn("creation", str(ctx.exception).lower())

    def test_fakes_model_github_creation_enforcement(self):
        # The fakes must model GitHub's documented enforcement: a create_ref
        # for a ref covered by a bypass-less `creation` rule is refused, the way
        # GitHub refuses it, so the compliant happy-path fixture (an `update`
        # rule) is one GitHub would actually accept rather than one that only
        # passes because the fake ignores rules.
        client = RecordingClient()
        summary = {"id": 9, "name": "c", "target": "tag", "enforcement": "active"}
        detail = {
            "id": 9,
            "target": "tag",
            "enforcement": "active",
            "current_user_can_bypass": "never",
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "creation"}, {"type": "deletion"}],
        }
        client.rulesets = [summary]
        client.ruleset_details = {9: detail}
        with self.assertRaises(RuntimeError):
            client.create_ref(ref="refs/tags/v0.1.0", sha="t" * 40)
        # And deletion is refused too (GitHub's default rule).
        client.existing_refs["tags/v0.1.0"] = "t" * 40
        with self.assertRaises(RuntimeError):
            client.delete_ref("tags/v0.1.0")

    def test_fakes_allow_update_rule_ref_writes(self):
        # The compliant fixture (update-only, bypass never) must let the
        # workflow's create and delete succeed, since `update` restricts only
        # moving an existing tag.
        client = RecordingClient()
        apply_release_settings(client)
        client.create_ref(ref="refs/tags/v0.1.0", sha="t" * 40)  # no raise
        client.delete_ref("tags/v0.1.0")  # no raise

    def test_fakes_aggregate_two_rulesets_for_a_ref_write(self):
        # The fakes must aggregate the rules of EVERY covering ruleset the way
        # GitHub does: an update-only v* ruleset plus a ~ALL deletion ruleset
        # means the create succeeds (no creation rule) but the cleanup delete is
        # refused (the deletion rule from the second ruleset still applies). A
        # first-match fake that only consulted the update ruleset would wrongly
        # allow the delete.
        client = RecordingClient()
        client.rulesets = [
            {"id": 11, "name": "update v*", "target": "tag", "enforcement": "active"},
            {"id": 12, "name": "all deletion", "target": "tag", "enforcement": "active"},
        ]
        client.ruleset_details = {
            11: {
                "id": 11,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                "rules": [{"type": "update"}],
            },
            12: {
                "id": 12,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
                "rules": [{"type": "deletion"}],
            },
        }
        client.create_ref(ref="refs/tags/v0.1.0", sha="t" * 40)  # no creation rule: allowed
        with self.assertRaises(RuntimeError):
            client.delete_ref("tags/v0.1.0")  # deletion rule from the ~ALL ruleset applies

    def _fake_with_creation_bypass(self, bypass):
        # A client carrying one active v* tag ruleset with a `creation` rule and
        # the given ``current_user_can_bypass`` value.
        client = RecordingClient()
        client.rulesets = [{"id": 7, "name": "v creation", "target": "tag", "enforcement": "active"}]
        client.ruleset_details = {
            7: {
                "id": 7,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": bypass,
                "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                "rules": [{"type": "creation"}],
            }
        }
        return client

    def test_fakes_treat_exempt_as_a_bypass(self):
        # `exempt` is a bypass value (GitHub does not run the rules for the
        # actor), so the fakes must ALLOW the create a bypass-less identity
        # would be refused. A fake that treated `exempt` as "not a bypass"
        # would wrongly refuse this write.
        client = self._fake_with_creation_bypass("exempt")
        client.create_ref(ref="refs/tags/v0.1.0", sha="t" * 40)  # no raise

    def test_fakes_treat_pull_requests_only_as_not_a_bypass(self):
        # `pull_requests_only` bypasses only on a pull request; a direct
        # refs/tags/<tag> write is not one, so the fakes must REFUSE the create
        # the way GitHub would. A fake that treated `pull_requests_only` as a
        # bypass would wrongly allow this write.
        client = self._fake_with_creation_bypass("pull_requests_only")
        with self.assertRaises(RuntimeError):
            client.create_ref(ref="refs/tags/v0.1.0", sha="t" * 40)

    def test_matcher_table_matches_github_fnm_pathname(self):
        # Pin every row of the matcher table against GitHub's
        # documented File.fnmatch + FNM_PATHNAME semantics (* does not cross /,
        # **/ matches zero or more directories, ~ALL matches everything). Each
        # row lists the include pattern(s), the dispatched tag, and whether the
        # gate must pass. The fail-open rows (Python's fnmatch would accept
        # them) and the false-refusal rows (Python would reject them) are both
        # pinned so a regression in either direction fails.
        cases = [
            # (include, exclude, tag, expected_covers)
            (["refs/tags/v*"], [], "v0.1.0", True),
            (["refs/tags/v*"], [], "v1.0.0", True),
            (["refs/tags/v*"], [], "v0.1.0-rc.1", True),
            (["refs/tags/v*"], [], "v10.2.3", True),
            (["refs/tags/v[0-9]*"], [], "v0.1.0", True),
            (["~ALL"], [], "v1.0.0", True),
            (["refs/tags/untagged-*"], [], "v0.1.0", False),
            # Subset includes: fail-open under Python's fnmatch, correct here.
            (["refs/tags/v0.*"], [], "v1.0.0", False),
            (["refs/tags/v0.*"], [], "v0.1.0", True),
            (["refs/tags/v0.0.0"], [], "v0.1.0", False),
            # v* minus an rc exclude: the rc tag is NOT covered.
            (["refs/tags/v*"], ["refs/tags/v*-rc.*"], "v0.1.0-rc.1", False),
            (["refs/tags/v*"], ["refs/tags/v*-rc.*"], "v0.1.0", True),
            # v* minus v1*: v1.0.0 and v10.2.3 are NOT covered.
            (["refs/tags/v*"], ["refs/tags/v1*"], "v1.0.0", False),
            (["refs/tags/v*"], ["refs/tags/v1*"], "v0.1.0", True),
            # refs/* does NOT cross '/', so it does not cover a two-segment tag ref.
            (["refs/*"], [], "v0.1.0", False),
            # refs/tags/**/* DOES cover a tag (**/ matches zero or more dirs).
            (["refs/tags/**/*"], [], "v0.1.0", True),
            # ~ALL minus refs/*: refs/* does not match the tag ref, so ~ALL still covers.
            (["~ALL"], ["refs/*"], "v0.1.0", True),
            # A `**/` that does NOT start a path segment is a single-segment `*`
            # (not "zero or more directories"), so a trailing `/**/*` or `/*`
            # then needs a `/` the tag ref does not have. These cover no tag.
            (["refs/tags/v**/**/*"], [], "v0.1.0", False),
            (["refs/tags/v**/*"], [], "v0.1.0", False),
            # `ref**` is a single-segment match, so `ref**/v*` needs `refs/v...`,
            # which the two-segment tag ref is not.
            (["ref**/v*"], [], "v0.1.0", False),
            # A bracket set never matches `/` under FNM_PATHNAME, so `[/]` cannot
            # join the two segments; the pattern covers no tag.
            (["refs/tags[/]v*"], [], "v0.1.0", False),
            # A backslash escapes the next character (fnmatch default), so `\\v`
            # is a literal `v` and the pattern DOES cover the tag ref.
            (["refs/tags/\\v*"], [], "v0.1.0", True),
            # A bracket set ends at the FIRST `]` (Ruby), so `[]v]*` is an empty
            # set (matches no character) followed by the literals `v]`; it covers
            # no tag ref. Python's own fnmatch would instead treat the leading
            # `]` as a set member and fail open here.
            (["refs/tags/[]v]*"], [], "v0.1.0", False),
            # `[!]]*` is a negated-empty set (matches any one character) followed
            # by the literals `]`; `refs/tags/<char>]...` never matches a tag ref,
            # so it covers no tag. Python's fnmatch would fail open.
            (["refs/tags/[!]]*"], [], "v0.1.0", False),
            # The empty set `[]...` alone matches NO character (Ruby ends the set
            # at the first `]`), so `[]0.1.0` consumes no character and then needs
            # a literal `0.1.0` the ref `v0.1.0` does not start with: no coverage.
            # (If `[]` were read as "matches any character", this would be True.)
            (["refs/tags/[]0.1.0"], [], "v0.1.0", False),
            # The negated-empty set `[!]...` matches ANY one character, so
            # `[!]0.1.0` matches the leading `v` and then `0.1.0`: it covers the
            # tag ref. (If `[!]` matched nothing, this would be False.)
            (["refs/tags/[!]0.1.0"], [], "v0.1.0", True),
        ]
        for include, exclude, tag, expected in cases:
            with self.subTest(include=include, exclude=exclude, tag=tag):
                client = self._ruleset(include, exclude=exclude)
                # Observe coverage through the gate's own code path: a covering
                # ruleset carries a restricting rule (the default fixture rules
                # are creation/deletion), so coverage holds iff
                # `_covering_tag_rulesets` returns a restricting ruleset for the
                # dispatched ref.
                ref = f"refs/tags/{tag}"
                covering = validate_mod._covering_tag_rulesets(client, ref=ref)
                covers = any(
                    validate_mod._ruleset_rule_types(detail) & validate_mod._TAG_RESTRICTING_RULE_TYPES
                    for detail in covering
                )
                self.assertEqual(
                    covers,
                    expected,
                    f"include={include} exclude={exclude} tag={tag}",
                )


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

    def test_tag_typed_main_policy_fails(self):
        # A deployment branch policy entry named "main" but typed "tag" admits
        # the TAG main, not the branch, so it must not satisfy the main-only
        # branch requirement; the gate must fail closed.
        client = self._client()
        client.deployment_branch_policies["release"] = [{"id": 1, "name": "main", "type": "tag"}]
        with self.assertRaises(validate_mod.ValidationError) as ctx:
            validate_mod.validate_environment_policy(client)
        self.assertIn("branch policy", str(ctx.exception).lower())

    def test_branch_typed_main_policy_passes(self):
        # An explicit type "branch" is accepted (as is an absent type).
        client = self._client()
        client.deployment_branch_policies["release"] = [{"id": 1, "name": "main", "type": "branch"}]
        validate_mod.validate_environment_policy(client)  # does not raise

    def test_report_flags_tag_typed_main_branch_policy(self):
        # report_environment_policy applies the same branch-typed main check as
        # the gate: a policy entry named "main" but typed "tag" admits the TAG
        # main, not the branch, so the report must say branch_policy=missing. A
        # names-only comparison in the report would wrongly say ok.
        client = self._client()
        client.deployment_branch_policies["release"] = [{"id": 1, "name": "main", "type": "tag"}]
        report = validate_mod.report_environment_policy(client)
        self.assertEqual(report["branch_policy"], "missing")

    def test_report_accepts_branch_typed_main_branch_policy(self):
        client = self._client()
        client.deployment_branch_policies["release"] = [{"id": 1, "name": "main", "type": "branch"}]
        report = validate_mod.report_environment_policy(client)
        self.assertEqual(report["branch_policy"], "ok")


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
            with mock.patch.object(publish, "_sleep", lambda _s: None):
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
            with mock.patch.object(publish, "_sleep", lambda _s: None):
                with self.assertRaises(validate_mod.ValidationError):
                    publish.publish_release_from_dir(client, out, _metadata())
            self.assertNotIn("delete_release", client.calls)
            self.assertNotIn("delete_ref", client.calls)
            self.assertTrue(any(not r.get("draft", True) for r in client.releases))

    def test_stale_draft_read_after_successful_publish_deletes_nothing(self):
        # A stale-read scenario: the publish PATCH succeeded (result.published is
        # True), but get_release / list_releases return a STALE draft:true read.
        # The read-back fails "still a draft", yet because the PATCH already
        # succeeded cleanup must delete NOTHING. If cleanup re-read live state
        # (instead of trusting result.published) it would see a draft and delete
        # the release and tag ref -- destroying a published release.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)

            class StaleDraftReadClient(RecordingClient):
                def update_release(self, release_id, *, draft=None, prerelease=None, make_latest=None):
                    # The PATCH succeeds server-side (recorded), but the record
                    # keeps draft=True so every later READ is a stale draft read.
                    self.calls.append("update_release")
                    return {"id": release_id, "draft": True}

                def get_release(self, release_id):
                    payload = super().get_release(release_id)
                    payload["draft"] = True  # stale read
                    return payload

                def list_releases(self):
                    out = super().list_releases()
                    for rec in out:
                        rec["draft"] = True  # stale read
                    return out

            client = StaleDraftReadClient()
            client.main_head = COMMIT
            apply_release_settings(client)
            with mock.patch.object(publish, "_sleep", lambda _s: None):
                with self.assertRaises(validate_mod.ValidationError) as ctx:
                    publish.publish_release_from_dir(client, out, _metadata())
            self.assertIn("draft", str(ctx.exception).lower())
            # Despite the live state reading as a draft, nothing was deleted.
            self.assertNotIn("delete_release", client.calls)
            self.assertNotIn("delete_ref", client.calls)


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

    def test_lagging_draft_asset_list_is_retried_before_the_tag(self):
        # The pre-tag draft asset list reads through the retry helper, so a
        # list that lags once (returns fewer than four assets) immediately
        # after the uploads is retried rather than failing the run before the
        # tag exists.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            real_list = client.list_release_assets
            state = {"n": 0}

            def flaky_list(release_id):
                state["n"] += 1
                if state["n"] == 1:
                    return []  # the list lags once right after upload
                return real_list(release_id)

            client.list_release_assets = flaky_list  # type: ignore[assignment]
            with mock.patch.object(publish, "_sleep", lambda _s: None):
                result = publish.publish_release_from_dir(client, out, _metadata())
            self.assertTrue(result.published)
            self.assertGreaterEqual(state["n"], 2)


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

    def test_https_to_http_same_host_downgrade_is_refused(self):
        # A same-host redirect that downgrades https -> http would carry the
        # bearer token in cleartext; the handler must refuse it and keep the
        # refused target in the message.
        # Standard library
        import urllib.request

        # Local modules
        from release.github_client import _NoCrossHostRedirect

        handler = _NoCrossHostRedirect()
        req = urllib.request.Request("https://api.github.com/repos/o/r/releases/1")
        with self.assertRaises(Exception) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://api.github.com/repos/o/r/releases/1")
        self.assertIn("http://api.github.com", str(ctx.exception))

    def test_real_transport_refuses_cross_host_and_keeps_token_off_the_second_host(self):
        # Through the REAL urllib transport (not the handler in isolation): a
        # local server redirects to a second local server on another port. The
        # client must refuse the redirect (so the second server never sees the
        # Authorization header) and the error must name the refused target.
        # Standard library
        import http.server
        import threading

        # Local modules
        from release.github_client import GitHubClient, GitHubError

        seen_headers: list[dict] = []

        def make_server(handler_cls):
            server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, thread

        # Second host: records whether it ever received the bearer token.
        class SecondHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):  # silence
                pass

        second_server, second_thread = make_server(SecondHandler)
        second_port = second_server.server_address[1]

        # First host: redirects to the second host (different port => different netloc).
        class FirstHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{second_port}/elsewhere")
                self.end_headers()

            def log_message(self, *_args):
                pass

        first_server, first_thread = make_server(FirstHandler)
        first_port = first_server.server_address[1]

        try:
            client = GitHubClient(
                owner="o",
                repo="r",
                token="secret-token",  # noqa: S106 - test token
                api_root=f"http://127.0.0.1:{first_port}",
            )
            with self.assertRaises(GitHubError) as ctx:
                client.get_release(1)
            # The refused target (the second host) is named in the error.
            self.assertIn(f"127.0.0.1:{second_port}", str(ctx.exception))
            # The second server never received the request (so never the token).
            self.assertEqual(seen_headers, [])
        finally:
            first_server.shutdown()
            second_server.shutdown()
            first_thread.join(timeout=5)
            second_thread.join(timeout=5)
            first_server.server_close()
            second_server.server_close()


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

    def test_still_a_draft_check_rejects_a_draft_readback(self):
        # If the release reads back as still a draft, the read-back must fail.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "release-artifacts"
            _write_artifact_set(out)
            client = _ready_client()

            def no_publish(release_id, *, draft=None, prerelease=None, make_latest=None):
                # Record the call but leave the release a draft.
                client.calls.append("update_release")
                return {"id": release_id, "draft": True}

            client.update_release = no_publish  # type: ignore[assignment]
            with mock.patch.object(publish, "_sleep", lambda _s: None):
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
        with mock.patch.object(publish, "_sleep", lambda _s: None):
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
        with mock.patch.object(publish, "_sleep", lambda _s: None):
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

    def test_reports_would_block_for_unbypassable_rule(self):
        # A covering ruleset with a creation rule the token cannot bypass prints
        # "would-block" and the before-approval note, so the dry run warns that
        # the publish gate would refuse before the draft.
        # Standard library
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        # Local modules
        from release import cli

        client = _ready_client()
        client.rulesets = [{"id": 71, "name": "v creation", "target": "tag", "enforcement": "active"}]
        client.ruleset_details = {
            71: {
                "id": 71,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                "rules": [{"type": "creation"}],
            }
        }
        buf = io.StringIO()
        with (
            mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
            redirect_stdout(buf),
        ):
            cli.cmd_verify_environment(mock.MagicMock())
        out = buf.getvalue()
        self.assertIn("v* tag ruleset=would-block", out)
        self.assertIn("cannot bypass", out)

    def test_report_uses_the_dispatched_tag_from_the_environment(self):
        # The settings report must be built with the dispatched GBAW_RELEASE_TAG,
        # not an empty placeholder. A ruleset covers only v1.* (an update rule),
        # so the dispatched tag v1.0.0 is reported present; dropping the tag at
        # the call site (an empty tag -> refs/tags/v, which v1.* does not cover)
        # would report absent, so this fails if the tag is not threaded through.
        # Standard library
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        # Local modules
        from release import cli

        client = _ready_client()
        client.rulesets = [{"id": 88, "name": "v1", "target": "tag", "enforcement": "active"}]
        client.ruleset_details = {
            88: {
                "id": 88,
                "target": "tag",
                "enforcement": "active",
                "current_user_can_bypass": "never",
                "conditions": {"ref_name": {"include": ["refs/tags/v1.*"], "exclude": []}},
                "rules": [{"type": "update"}],
            }
        }
        buf = io.StringIO()
        with (
            mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
            mock.patch.dict("os.environ", {"GBAW_RELEASE_TAG": "v1.0.0"}, clear=False),
            redirect_stdout(buf),
        ):
            cli.cmd_verify_environment(mock.MagicMock())
        self.assertIn("v* tag ruleset=present", buf.getvalue())

    def test_environment_policy_report_is_printed(self):
        # cmd_verify_environment must print the environment-policy report
        # (prevent-self-review and the main-only branch policy). A compliant
        # environment reports ok; a non-compliant one reports missing and emits
        # the note. This pins report_environment_policy's result and its
        # branch-policy check, which a hardcoded "ok" or a dropped branch check
        # would otherwise leave untested.
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
            cli.cmd_verify_environment(mock.MagicMock())
        out = buf.getvalue()
        self.assertIn("prevent-self-review=ok", out)
        self.assertIn("main-only branch policy=ok", out)

    def test_environment_policy_report_flags_a_non_compliant_environment(self):
        # Standard library
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        # Local modules
        from release import cli

        client = _ready_client()
        # Self-review is allowed and the branch policy is not main-only.
        client.environments["release"]["protection_rules"][0]["prevent_self_review"] = False
        client.deployment_branch_policies["release"] = [{"id": 1, "name": "main"}, {"id": 2, "name": "release/*"}]
        buf = io.StringIO()
        with (
            mock.patch.object(cli, "_client", return_value=(client, "o", "r")),
            redirect_stdout(buf),
        ):
            cli.cmd_verify_environment(mock.MagicMock())
        out = buf.getvalue()
        self.assertIn("prevent-self-review=missing", out)
        self.assertIn("main-only branch policy=missing", out)
        self.assertIn("fails closed unless the release environment prevents self-review", out)


if __name__ == "__main__":
    unittest.main()
