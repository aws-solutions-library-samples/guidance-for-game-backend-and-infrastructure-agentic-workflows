"""Shared in-memory fakes for release tooling tests (stdlib only).

Importing release modules requires ``scripts/`` on ``sys.path`` so that
``release`` resolves as a package. Test modules insert that path themselves.
"""

from __future__ import annotations

# Standard library
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Local modules
from release.github_client import GitHubClient, Request, Response  # noqa: E402


class RecordingClient(GitHubClient):
    """A GitHubClient whose transport is a programmable in-memory fake.

    Instead of a network transport, callers populate ``responses`` and the
    client records every ``Request`` it would have sent in ``calls``. The
    higher-level methods (list_releases, create_release, ...) are overridden to
    read from simple attributes so tests stay declarative, while the recording
    of mutating calls still lets ordering tests assert write sequencing.
    """

    def __init__(self, owner="aws-solutions-library-samples", repo="guidance-x", token="t"):  # noqa: S107 - fake token
        super().__init__(owner=owner, repo=repo, token=token, transport=self._record)
        self.calls: list[str] = []
        # Read state
        self.main_head = "a" * 40
        self.existing_refs: dict[str, str] = {}  # "tags/vX" -> object sha
        self.releases: list[dict] = []
        self.tags: list[str] = []
        self.check_runs: dict[str, list[dict]] = {}  # name -> runs
        self.comments: dict[int, dict] = {}
        self.environments: dict[str, dict] = {}  # name -> environment payload
        self.branch_rules: list[dict] = []  # GET /repos/{o}/{r}/rules/branches/{b}
        self.collaborator_permissions: dict[str, str] = {}  # login -> permission
        # Write behavior
        self.fail_on: str | None = None  # method name to raise in
        self._next_release_id = 100
        self._tag_object_sha = "t" * 40

    def _record(self, request: Request) -> Response:  # pragma: no cover - unused
        raise AssertionError("RecordingClient must not hit the network")

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise RuntimeError(f"injected failure in {name}")

    # -- reads --
    def get_branch_head_sha(self, branch: str) -> str:
        self.calls.append(f"get_branch_head_sha:{branch}")
        return self.main_head

    def get_ref(self, ref: str):
        self.calls.append(f"get_ref:{ref}")
        sha = self.existing_refs.get(ref)
        if sha is None:
            return None
        return {"ref": f"refs/{ref}", "object": {"sha": sha}}

    def list_tags(self):
        self.calls.append("list_tags")
        return list(self.tags)

    def list_releases(self):
        self.calls.append("list_releases")
        return [dict(r) for r in self.releases]

    def list_check_runs(self, commit: str, *, check_name=None):
        self.calls.append(f"list_check_runs:{check_name}")
        return list(self.check_runs.get(check_name, []))

    def get_issue_comment(self, comment_id: int):
        self.calls.append(f"get_issue_comment:{comment_id}")
        return dict(self.comments[comment_id])

    def get_environment(self, name: str):
        self.calls.append(f"get_environment:{name}")
        environments = getattr(self, "environments", None) or {}
        payload = environments.get(name)
        return dict(payload) if payload is not None else None

    def get_branch_rules(self, branch: str):
        self.calls.append(f"get_branch_rules:{branch}")
        return [dict(rule) for rule in getattr(self, "branch_rules", [])]

    def get_collaborator_permission(self, username: str) -> str:
        self.calls.append(f"get_collaborator_permission:{username}")
        return getattr(self, "collaborator_permissions", {}).get(username, "none")

    # -- writes --
    def create_release(self, *, tag_name, target_commitish, name, body, draft, prerelease):
        self._maybe_fail("create_release")
        rid = self._next_release_id
        self._next_release_id += 1
        rec = {
            "id": rid,
            "tag_name": tag_name,
            "draft": draft,
            "prerelease": prerelease,
            "upload_url": f"https://uploads.example.invalid/releases/{rid}/assets{{?name,label}}",
        }
        self.releases.append(rec)
        return rec

    def update_release(self, release_id, *, draft=None, prerelease=None, make_latest=None):
        self._maybe_fail("update_release")
        for rec in self.releases:
            if rec["id"] == release_id:
                if draft is not None:
                    rec["draft"] = draft
                if make_latest is not None:
                    rec["make_latest"] = make_latest
                return dict(rec)
        raise KeyError(release_id)

    def delete_release(self, release_id):
        self._maybe_fail("delete_release")
        self.releases = [r for r in self.releases if r["id"] != release_id]

    def upload_release_asset(self, upload_url, *, name, data, content_type):
        self._maybe_fail(f"upload_release_asset:{name}")
        return {"name": name, "state": "uploaded"}

    def create_tag_object(self, *, tag, message, commit, tagger_name, tagger_email, date):
        self._maybe_fail("create_tag_object")
        return self._tag_object_sha

    def create_ref(self, *, ref, sha):
        self._maybe_fail("create_ref")
        key = ref.replace("refs/", "", 1)
        self.existing_refs[key] = sha
        return {"ref": ref, "object": {"sha": sha}}

    def delete_ref(self, ref):
        self._maybe_fail("delete_ref")
        self.existing_refs.pop(ref, None)


def completed_check(name: str, commit: str, conclusion="success", app_id=15368, completed_at="2024-01-01T00:00:00Z"):
    return {
        "name": name,
        "head_sha": commit,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": app_id},
        "completed_at": completed_at,
    }
