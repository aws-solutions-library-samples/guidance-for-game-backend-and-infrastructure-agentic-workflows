"""Publication-ordering contracts with a fake GitHub client."""

# Standard library
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


def _inputs(tag="v0.1.0"):
    arts, sums = artifacts_mod.assemble_artifacts(
        manifest_bytes=b'{"schema_version": "gbaw.release-manifest.v1"}\n',
        backend_sbom_bytes=b'{"spdx": "backend"}\n',
        frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
    )
    return publish.PublishInputs(
        tag=tag,
        commit=COMMIT,
        release_name=tag,
        notes="# notes\n## Defaults\n",
        artifacts=arts,
        sha256sums=sums,
        tagger_name="Release Bot",
        tagger_email="release@example.com",
        commit_date="2024-01-02T03:04:05Z",
    )


def _ready_client():
    client = RecordingClient()
    client.main_head = COMMIT
    return client


class PublicationOrderingTests(unittest.TestCase):
    def test_happy_path_order(self):
        client = _ready_client()
        result = publish.publish_release(client, _inputs())
        self.assertTrue(result.published)
        # Draft creation must precede any tag write.
        draft_idx = client.calls.index("create_release")
        tag_obj_idx = client.calls.index("create_tag_object")
        ref_idx = client.calls.index("create_ref")
        publish_idx = len(client.calls) - 1 - client.calls[::-1].index("update_release")
        self.assertLess(draft_idx, tag_obj_idx)
        self.assertLess(tag_obj_idx, ref_idx)
        self.assertLess(ref_idx, publish_idx)

    def test_no_tag_write_before_validation_preflight(self):
        client = _ready_client()
        client.main_head = "f" * 40  # HEAD moved
        with self.assertRaises(Exception):
            publish.publish_release(client, _inputs())
        self.assertNotIn("create_tag_object", client.calls)
        self.assertNotIn("create_ref", client.calls)
        self.assertNotIn("create_release", client.calls)

    def test_tag_absence_rechecked_in_preflight(self):
        client = _ready_client()
        client.existing_refs["tags/v0.1.0"] = "deadbeef"
        with self.assertRaises(Exception):
            publish.publish_release(client, _inputs())
        self.assertNotIn("create_release", client.calls)

    def test_draft_deleted_when_asset_upload_fails(self):
        client = _ready_client()
        client.fail_on = "upload_release_asset:release-manifest.json"
        with self.assertRaises(RuntimeError):
            publish.publish_release(client, _inputs())
        self.assertIn("delete_release", client.calls)
        self.assertEqual(client.releases, [])  # draft cleaned up
        self.assertNotIn("create_tag_object", client.calls)

    def test_tag_ref_cleaned_up_when_publish_fails(self):
        client = _ready_client()
        client.fail_on = "update_release"  # fail at the final publish step
        with self.assertRaises(RuntimeError):
            publish.publish_release(client, _inputs())
        # Our run created the ref; it must be deleted since publish failed.
        self.assertIn("create_ref", client.calls)
        self.assertIn("delete_ref", client.calls)
        self.assertNotIn("tags/v0.1.0", client.existing_refs)
        self.assertEqual(client.releases, [])  # draft also removed

    def test_prerelease_flag_for_rc(self):
        client = _ready_client()
        result = publish.publish_release(client, _inputs(tag="v0.1.0-rc.1"))
        self.assertTrue(result.published)
        self.assertTrue(client.releases[0]["prerelease"])

    def test_never_partial_publish_leaves_published_release(self):
        client = _ready_client()
        client.fail_on = "create_ref"
        with self.assertRaises(RuntimeError):
            publish.publish_release(client, _inputs())
        # No published (non-draft) release may remain.
        self.assertFalse(any(not r.get("draft", True) for r in client.releases))


if __name__ == "__main__":
    unittest.main()
