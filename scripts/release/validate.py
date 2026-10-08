"""Pre-write release validation. Nothing here mutates the repository.

Every check runs before the publish job touches a tag or release. The checks
bind the proposed tag, the dispatched commit, the live ``main`` HEAD, the
required status checks, and the deployed-validation evidence comment to one
another so a stale, reused, moved, or unverified release cannot proceed.

The validator takes a :class:`~scripts.release.github_client.GitHubClient`
(real or fake) plus the local repository root. It returns a structured result;
the caller decides whether to continue to artifact generation.
"""

from __future__ import annotations

# Standard library
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence as evidence_mod
from . import semver
from .github_client import GitHubClient

# The required checks on ``main`` are read live from the branch ruleset
# (``GET /repos/{owner}/{repo}/rules/branches/main``) at validation time, so a
# renamed or newly added required check is enforced automatically. This tuple
# is retained only as human documentation of the expected set at authoring
# time; it is NOT the source of truth. ``GITHUB_ACTIONS_APP_ID`` is the default
# integration id used only when the ruleset entry omits one.
REQUIRED_CHECK_NAMES = (
    "backend-tests",
    "e2e-tests",
    "frontend-tests",
    "powershell-tests (ubuntu-latest)",
    "powershell-tests (windows-latest)",
    "vulnerability-scan",
    "codeql-required",
)
GITHUB_ACTIONS_APP_ID = 15368

MAIN_BRANCH = "main"

# The deployment environment the publish job runs in. It must exist and carry a
# required-reviewers protection rule before any non-dry run may write a tag or
# release; otherwise a first dispatch would publish into an auto-created,
# unprotected environment with no human approval.
RELEASE_ENVIRONMENT = "release"
_REQUIRED_REVIEWERS_RULE = "required_reviewers"

# Repository permission levels that count as "maintains this repository".
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

RELEASE_NOTES_SECTIONS = (
    "defaults",
    "capabilities",
    "known issues",
    "compatibility",
    "deployment",
    "rollback",
)


class ValidationError(RuntimeError):
    """A single fatal validation failure."""


@dataclass
class CheckRunConclusion:
    name: str
    conclusion: str


@dataclass
class ValidationResult:
    tag: str
    commit: str
    release_name: str
    evidence_digest: str
    gate_summary: dict[str, str]
    waivers: dict[str, str] = field(default_factory=dict)
    check_conclusions: list[CheckRunConclusion] = field(default_factory=list)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def validate_tag_grammar(tag: str) -> semver.ReleaseTag:
    try:
        return semver.parse_tag(tag)
    except semver.InvalidTagError as error:
        raise ValidationError(str(error)) from error


def validate_tag_available(client: GitHubClient, parsed: semver.ReleaseTag) -> None:
    tag = parsed.raw
    # No ref with this tag name may exist (covers a moved/stale ref).
    _require(client.get_ref(f"tags/{tag}") is None, f"tag ref already exists: {tag}")

    # No release or draft may already use this tag name.
    for release in client.list_releases():
        if str(release.get("tag_name")) == tag:
            state = "draft" if release.get("draft") else "release"
            raise ValidationError(f"a {state} already uses tag {tag}")

    # Strictly greater than every existing v* tag (so a deleted or older name
    # cannot be reused to look newer).
    existing = client.list_tags()
    _require(
        semver.strictly_greater_than_all(tag, existing),
        f"tag {tag} is not strictly greater than every existing v* tag",
    )


def validate_commit_is_main_head(client: GitHubClient, commit: str) -> None:
    _require(
        len(commit) == 40 and all(c in "0123456789abcdef" for c in commit),
        "commit must be a 40-char lowercase hex sha",
    )
    head = client.get_branch_head_sha(MAIN_BRANCH)
    _require(head == commit, "commit does not equal current main HEAD")


def _read_required_contexts(client: GitHubClient) -> list[tuple[str, int | None]]:
    """Read the authoritative required status checks from the live branch ruleset.

    Returns a list of ``(context, integration_id)`` pairs from the
    ``required_status_checks`` rule on ``main``. Fails closed if the ruleset is
    missing or declares no required checks (a release must never validate
    against an empty or absent required-check policy).
    """

    rules = client.get_branch_rules(MAIN_BRANCH)
    contexts: list[tuple[str, int | None]] = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        for entry in params.get("required_status_checks") or []:
            if not isinstance(entry, dict):
                continue
            context = entry.get("context")
            if not isinstance(context, str) or not context:
                continue
            integration = entry.get("integration_id")
            contexts.append((context, int(integration) if isinstance(integration, int) else None))
    _require(bool(contexts), "no required status checks found in the main branch ruleset")
    return contexts


def _run_sort_key(run: dict) -> tuple[str, str, int]:
    """Order runs newest-last by start time, then completion, then id.

    GitHub records ``started_at`` on every check run; an in-progress rerun
    therefore sorts after an earlier completed run even though it has no
    ``completed_at``. ``completed_at`` is a secondary key so historical fixtures
    that set only it still order correctly, and ``id`` is a final tie-break.
    """

    started = str(run.get("started_at") or "")
    completed = str(run.get("completed_at") or "")
    run_id = run.get("id")
    return (started, completed, int(run_id) if isinstance(run_id, int) else 0)


def _latest_successful(runs: list[dict], *, context: str, commit: str, integration_id: int | None) -> None:
    """Fail closed unless ``context`` has a usable successful latest run.

    A run counts only when it names the context, carries a present ``head_sha``
    equal to the release commit, and a present ``app.id`` equal to the required
    integration (GitHub Actions). Runs with a ``skipped`` conclusion -- which
    scheduled workflow runs record on the same commit -- are ignored when
    choosing the latest run, so a later skip never masks an earlier success.
    The newest non-skipped run (by ``started_at``, then ``completed_at``, then
    ``id``) must be completed and successful, so a newer in-progress rerun of a
    required check blocks publication until it finishes rather than letting an
    older success stand in for it.
    """

    usable: list[dict] = []
    for run in runs:
        if run.get("name") != context:
            continue
        head_sha = run.get("head_sha")
        if not isinstance(head_sha, str) or head_sha != commit:
            continue  # fail closed: missing or wrong head_sha
        app = run.get("app")
        app_id = app.get("id") if isinstance(app, dict) else None
        if not isinstance(app_id, int):
            continue  # fail closed: missing app id
        expected = integration_id if integration_id is not None else GITHUB_ACTIONS_APP_ID
        if app_id != expected:
            continue
        usable.append(run)

    _require(bool(usable), f"required check missing on commit: {context}")
    # Ignore skipped runs when selecting the latest; a scheduled skip on the
    # same commit must not win over an earlier real success.
    non_skipped = [run for run in usable if run.get("conclusion") != "skipped"]
    _require(bool(non_skipped), f"required check has only skipped runs: {context}")
    latest = max(non_skipped, key=_run_sort_key)
    _require(latest.get("status") == "completed", f"required check not completed: {context}")
    _require(latest.get("conclusion") == "success", f"required check not successful: {context}")


def validate_required_checks(client: GitHubClient, commit: str) -> list[CheckRunConclusion]:
    conclusions: list[CheckRunConclusion] = []
    for context, integration_id in _read_required_contexts(client):
        runs = client.list_check_runs(commit, check_name=context)
        _latest_successful(runs, context=context, commit=commit, integration_id=integration_id)
        conclusions.append(CheckRunConclusion(name=context, conclusion="success"))
    return conclusions


def validate_evidence_comment(
    client: GitHubClient,
    *,
    comment_id: int,
    commit: str,
    scan_fn,
    expected_issue: int | None = None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Validate the evidence comment and return (digest, gate_summary, waivers).

    ``scan_fn(text) -> list`` runs the public-content scanner over the raw
    comment body and must return an empty list for the comment to pass.
    ``expected_issue`` (the issue number from the dispatched permalink) must
    equal the issue the comment is actually on, so a permalink cannot name one
    issue while pointing at a comment on another. ``waivers`` maps each waived
    gate to the public issue reference that granted it, so the manifest can bind
    which issue authorized a waiver (not merely that the gate was waived).
    """

    comment = client.get_issue_comment(comment_id)
    # Must be a comment on an issue in THIS repository.
    issue_url = str(comment.get("issue_url", ""))
    issue_prefix = f"/repos/{client.owner}/{client.repo}/issues/"
    _require(issue_prefix in issue_url, "evidence comment is not on an issue in this repository")

    # The permalink's issue number must equal the comment's actual issue.
    if expected_issue is not None:
        actual_issue = issue_url.rsplit("/", 1)[-1]
        _require(
            actual_issue == str(expected_issue),
            "evidence comment permalink issue number does not match the comment's issue",
        )

    # The author must actually maintain this repository (write permission or
    # higher), confirmed via the collaborator-permission endpoint rather than
    # the broad author_association field.
    author = str((comment.get("user") or {}).get("login", ""))
    _require(bool(author), "evidence comment has no author login")
    permission = str(client.get_collaborator_permission(author))
    _require(
        permission in _WRITE_PERMISSIONS,
        "evidence comment author does not have write permission on this repository",
    )

    body = str(comment.get("body", ""))
    findings = scan_fn(body)
    _require(not findings, "evidence comment fails the public-content scan")

    document = evidence_mod.parse_and_validate(body, expected_commit=commit)
    block = evidence_mod.extract_json_block(body)
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    gate_summary = dict(document.gates)
    waivers = dict(document.waivers)
    return digest, gate_summary, waivers


def validate_environment_protected(client: GitHubClient, *, environment: str = RELEASE_ENVIRONMENT) -> None:
    """Fail closed unless the deployment environment is protected.

    The environment must already exist (a non-dry run must never publish into an
    environment GitHub would auto-create on first use) and must carry at least
    one ``required_reviewers`` protection rule with a non-empty reviewer list.
    Read-only: this consults the environments REST endpoint, which needs only
    ``actions: read``.
    """

    payload = client.get_environment(environment)
    _require(payload is not None, f"release environment '{environment}' does not exist; create and protect it first")
    rules = (payload or {}).get("protection_rules") or []
    if not isinstance(rules, list):
        raise ValidationError(f"release environment '{environment}' returned a malformed protection_rules payload")
    has_required_reviewers = any(
        isinstance(rule, dict)
        and str(rule.get("type", "")) == _REQUIRED_REVIEWERS_RULE
        and bool(rule.get("reviewers") or [])
        for rule in rules
    )
    _require(
        has_required_reviewers,
        f"release environment '{environment}' has no required-reviewers rule; add required reviewers first",
    )


def validate_release_notes(repo_root: Path, tag: str, *, scan_fn) -> None:
    notes = repo_root / "docs" / "releases" / f"{tag}.md"
    _require(notes.is_file(), f"release notes file missing: docs/releases/{tag}.md")
    text = notes.read_text(encoding="utf-8").lower()
    for section in RELEASE_NOTES_SECTIONS:
        _require(section in text, f"release notes missing required section: {section}")
    findings = scan_fn([str(notes)])
    _require(not findings, "release notes fail the public-content scan")


def run_full_validation(
    client: GitHubClient,
    repo_root: Path,
    *,
    commit: str,
    tag: str,
    evidence_comment_id: int,
    evidence_issue: int,
    release_name: str,
    comment_scan_fn,
    file_scan_fn,
) -> ValidationResult:
    """Run every pre-write check. Raises :class:`ValidationError` on first failure."""

    parsed = validate_tag_grammar(tag)
    validate_commit_is_main_head(client, commit)
    validate_tag_available(client, parsed)
    conclusions = validate_required_checks(client, commit)
    digest, gate_summary, waivers = validate_evidence_comment(
        client,
        comment_id=evidence_comment_id,
        commit=commit,
        scan_fn=comment_scan_fn,
        expected_issue=evidence_issue,
    )
    validate_release_notes(repo_root, tag, scan_fn=file_scan_fn)
    return ValidationResult(
        tag=tag,
        commit=commit,
        release_name=release_name,
        evidence_digest=digest,
        gate_summary=gate_summary,
        waivers=waivers,
        check_conclusions=conclusions,
    )
