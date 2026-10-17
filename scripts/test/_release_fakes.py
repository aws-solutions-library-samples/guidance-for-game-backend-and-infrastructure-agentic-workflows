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
        self.deployment_branch_policies: dict[str, list[dict]] = {}  # env name -> policies
        self.branch_rules: list[dict] = []  # GET /repos/{o}/{r}/rules/branches/{b}
        self.rulesets: list[dict] = []  # GET /repos/{o}/{r}/rulesets
        self.ruleset_details: dict[int, dict] = {}  # ruleset id -> full ruleset with conditions
        self.collaborator_permissions: dict[str, str] = {}  # login -> permission
        # Post-publish read-back state. Uploaded assets are recorded per release
        # id; a published release reads back immutable by default so the happy
        # path confirms. Tests flip these to exercise the read-back failures.
        self.release_assets: dict[int, list[dict]] = {}
        self.published_immutable = True
        self.tag_objects: dict[str, dict] = {}  # tag-object sha -> {"object": {...}}
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

    def list_rulesets(self, *, target=None):
        self.calls.append(f"list_rulesets:{target}")
        rulesets = [dict(r) for r in getattr(self, "rulesets", [])]
        if target is None:
            return rulesets
        return [r for r in rulesets if str(r.get("target", "")) == target]

    def get_ruleset(self, ruleset_id: int):
        self.calls.append(f"get_ruleset:{ruleset_id}")
        detail = getattr(self, "ruleset_details", {}).get(ruleset_id)
        if detail is not None:
            return dict(detail)
        # Fall back to the summary; a summary without conditions covers nothing.
        for summary in getattr(self, "rulesets", []):
            if summary.get("id") == ruleset_id:
                return dict(summary)
        raise KeyError(ruleset_id)

    def _ruleset_enforcement_for_ref(self, ref: str) -> "list[tuple[set[str], str]]":
        """Return ``(restricting rule types, current_user_can_bypass)`` for EVERY
        active tag ruleset that covers ``ref``, modeling GitHub's aggregation of
        the rules of every covering ruleset against a bypass-less identity.

        ``create_ref`` / ``delete_ref`` consult this so the fakes refuse a ref
        write GitHub would refuse: a ``creation`` rule in any covering ruleset
        blocks creating the ref, and a ``deletion`` rule in any covering ruleset
        blocks deleting it, unless the calling identity can bypass that same
        ruleset (``current_user_can_bypass`` is a bypass value). GitHub reports
        ``current_user_can_bypass`` per ruleset, so each covering ruleset is
        returned with its own value. This keeps the compliant happy-path fixture
        one GitHub would actually accept.
        """

        # Local modules
        from release.validate import _ruleset_pattern_matches_ref

        covering: list[tuple[set[str], str]] = []
        for summary in getattr(self, "rulesets", []):
            if str(summary.get("enforcement", "")) != "active" or str(summary.get("target", "")) != "tag":
                continue
            detail = getattr(self, "ruleset_details", {}).get(summary.get("id"), summary)
            if str(detail.get("enforcement", "")) != "active":
                continue
            conditions = (detail.get("conditions") or {}).get("ref_name") or {}
            include = [p for p in (conditions.get("include") or []) if isinstance(p, str)]
            exclude = [p for p in (conditions.get("exclude") or []) if isinstance(p, str)]
            if not any(_ruleset_pattern_matches_ref(p, ref) for p in include):
                continue
            if any(_ruleset_pattern_matches_ref(p, ref) for p in exclude):
                continue
            rule_types = {str(r.get("type", "")) for r in (detail.get("rules") or []) if isinstance(r, dict)}
            bypass = str(detail.get("current_user_can_bypass", "never"))
            covering.append((rule_types, bypass))
        return covering

    def _assert_ref_write_allowed(self, ref: str, rule_type: str) -> None:
        for rule_types, bypass in self._ruleset_enforcement_for_ref(ref):
            can_bypass = bypass in ("always", "exempt")
            if rule_type in rule_types and not can_bypass:
                raise RuntimeError(
                    f"ref write refused by ruleset {rule_type} rule: only bypass actors may {rule_type} {ref}"
                )

    def list_deployment_branch_policies(self, name: str):
        self.calls.append(f"list_deployment_branch_policies:{name}")
        return [dict(p) for p in getattr(self, "deployment_branch_policies", {}).get(name, [])]

    def get_release(self, release_id: int):
        self.calls.append(f"get_release:{release_id}")
        for rec in self.releases:
            if rec["id"] == release_id:
                payload = dict(rec)
                payload.setdefault("immutable", bool(self.published_immutable) and not rec.get("draft", True))
                return payload
        raise KeyError(release_id)

    def list_release_assets(self, release_id: int):
        self.calls.append(f"list_release_assets:{release_id}")
        return [dict(a) for a in self.release_assets.get(release_id, [])]

    def get_tag_object(self, sha: str):
        self.calls.append(f"get_tag_object:{sha}")
        payload = self.tag_objects.get(sha)
        if payload is None:
            # Default annotated tag object pointing at the current main head.
            return {"sha": sha, "object": {"sha": self.main_head, "type": "commit"}}
        return dict(payload)

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
        # Record the asset against the release id embedded in the upload_url so
        # the post-publish read-back can confirm name, size, and digest.
        # Standard library
        import hashlib

        rid = self._next_release_id - 1
        for rec in self.releases:
            if str(rec.get("upload_url", "")).startswith(upload_url.split("{", 1)[0]):
                rid = rec["id"]
                break
        self.release_assets.setdefault(rid, []).append(
            {
                "name": name,
                "size": len(data),
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
                "state": "uploaded",
            }
        )
        return {"name": name, "state": "uploaded"}

    def create_tag_object(self, *, tag, message, commit, tagger_name, tagger_email, date):
        self._maybe_fail("create_tag_object")
        sha = self._tag_object_sha
        self.tag_objects[sha] = {"sha": sha, "object": {"sha": commit, "type": "commit"}}
        return sha

    def create_ref(self, *, ref, sha):
        self._maybe_fail("create_ref")
        self._assert_ref_write_allowed(ref, "creation")
        key = ref.replace("refs/", "", 1)
        self.existing_refs[key] = sha
        return {"ref": ref, "object": {"sha": sha}}

    def delete_ref(self, ref):
        self._maybe_fail("delete_ref")
        full = ref if ref.startswith("refs/") else f"refs/{ref}"
        self._assert_ref_write_allowed(full, "deletion")
        key = ref.replace("refs/", "", 1)
        self.existing_refs.pop(key, None)


def completed_check(name: str, commit: str, conclusion="success", app_id=15368, completed_at="2024-01-01T00:00:00Z"):
    return {
        "name": name,
        "head_sha": commit,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": app_id},
        "completed_at": completed_at,
    }


def protected_environment(name: str = "release") -> dict:
    """A fully policy-compliant environment payload.

    Carries a required-reviewers rule with ``prevent_self_review: true`` and a
    deployment branch policy that enables custom branch policies, so the
    machine-checkable environment policy passes when paired with a ``main``-only
    branch-policy list (see :func:`apply_release_settings`).
    """

    return {
        "name": name,
        "protection_rules": [
            {
                "id": 1,
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "reviewer": {"login": "octo"}}],
            }
        ],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }


def covering_tag_ruleset(ruleset_id: int = 42) -> tuple[dict, dict]:
    """Return (summary, detail) for an active tag ruleset that covers v* tags.

    The detail carries an ``update`` rule, which restricts who may *move* a tag
    but leaves the workflow's own tag creation and cleanup deletion alone, so
    this is a setup GitHub would accept for a bypass-less workflow identity.
    ``current_user_can_bypass`` is ``never`` to match a token that is not a
    bypass actor; because only ``update`` is present, the gate still passes and
    the ref writes still succeed.
    """

    summary = {"id": ruleset_id, "name": "protect v*", "target": "tag", "enforcement": "active"}
    detail = {
        "id": ruleset_id,
        "name": "protect v*",
        "target": "tag",
        "enforcement": "active",
        "current_user_can_bypass": "never",
        "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        "rules": [{"type": "update"}],
    }
    return summary, detail


def apply_release_settings(client, *, environment_name: str = "release") -> None:
    """Make ``client`` satisfy the full non-dry publish policy.

    Sets a policy-compliant environment, a ``main``-only deployment branch
    policy, and an active tag ruleset covering ``refs/tags/v*`` with readable
    conditions, so a happy-path publish is not blocked by the fail-closed gates.
    """

    client.environments = {environment_name: protected_environment(environment_name)}
    client.deployment_branch_policies = {environment_name: [{"id": 1, "name": "main"}]}
    summary, detail = covering_tag_ruleset()
    client.rulesets = [summary]
    client.ruleset_details = {summary["id"]: detail}
