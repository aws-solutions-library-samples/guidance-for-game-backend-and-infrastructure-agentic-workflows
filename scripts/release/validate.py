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
import re
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


def validate_environment_policy(client: GitHubClient, *, environment: str = RELEASE_ENVIRONMENT) -> None:
    """Fail closed unless the environment's policy is fully machine-confirmable.

    Extends :func:`validate_environment_protected` with the two further rules
    that are readable with ``actions: read`` and so can be enforced rather than
    hand-checked before a non-dry run:

    * ``prevent_self_review`` is true on the required-reviewers rule, so the
      dispatcher cannot approve their own release;
    * the deployment branch policy is limited to ``main`` -- custom branch
      policies are enabled (not "any protected branch") and the only configured
      branch pattern is ``main``.

    Admin bypass has no documented field in the environment payload, so it
    remains a hand check in the before-first-run checklist.
    """

    validate_environment_protected(client, environment=environment)
    payload = client.get_environment(environment) or {}
    rules = payload.get("protection_rules") or []

    prevents_self_review = any(
        isinstance(rule, dict)
        and str(rule.get("type", "")) == _REQUIRED_REVIEWERS_RULE
        and rule.get("prevent_self_review") is True
        for rule in rules
    )
    _require(
        prevents_self_review,
        f"release environment '{environment}' does not prevent self-review; "
        "enable prevent-self-review so the dispatcher cannot approve their own release",
    )

    branch_policy = payload.get("deployment_branch_policy")
    _require(
        isinstance(branch_policy, dict) and branch_policy.get("custom_branch_policies") is True,
        f"release environment '{environment}' must restrict deployments to a custom branch policy limited to main",
    )
    policies = client.list_deployment_branch_policies(environment)
    names = sorted(str(p.get("name", "")) for p in policies)
    _require(
        names == [MAIN_BRANCH],
        f"release environment '{environment}' deployment branch policy must be exactly ['{MAIN_BRANCH}'], got {names}",
    )


# The ruleset rule types that restrict who may create, update, or delete a tag.
# A covering ruleset is only a real gate on tag writes when it carries at least
# one of these; a ruleset that merely matches the ref (for example one holding
# only ``non_fast_forward`` and ``required_deployments``) covers the tag without
# restricting who creates or updates it. The single ruleset GET returns ``rules``
# without write access, so this is machine-checked, not hand-checked.
_TAG_RESTRICTING_RULE_TYPES = frozenset({"creation", "update", "deletion"})


def _fnm_pathname_regex(pattern: str) -> str:
    """Translate a ruleset ref pattern to a regex with ``File.fnmatch`` +
    ``FNM_PATHNAME`` semantics.

    GitHub matches ``conditions.ref_name.include`` / ``exclude`` entries against
    a concrete ref with Ruby's ``File.fnmatch(pattern, ref, File::FNM_PATHNAME)``
    (see GitHub's "Creating rulesets" article). Under ``FNM_PATHNAME`` the
    wildcards ``*``, ``?``, and ``[...]`` never cross a ``/``, so ``refs/*`` does
    NOT match ``refs/tags/v1.0.0``. GitHub additionally treats a ``**`` path
    segment (``**/``) as zero or more directories, so ``refs/tags/**/*`` DOES
    match ``refs/tags/v1.0.0``. ``~ALL`` is the caller's special case and never
    reaches here. ``FNM_EXTGLOB`` and ``[^...]`` complement are unsupported by
    GitHub and are not emulated; a ``[!...]`` negation is honored as fnmatch
    documents it.
    """

    i = 0
    n = len(pattern)
    out = ["\\A"]
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:[^/]*/)*")  # zero or more directories
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append("[^/]*")  # trailing ** behaves as a single-segment *
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
                i += 1
            else:
                stuff = pattern[i + 1 : j]
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                stuff = stuff.replace("\\", "\\\\")
                out.append("[" + stuff + "]")
                i = j + 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("\\Z")
    return "".join(out)


def _ruleset_pattern_matches_ref(pattern: str, ref: str) -> bool:
    """Return True if a ruleset ``pattern`` matches ``ref`` the way GitHub does.

    ``~ALL`` matches every ref. Any other pattern is matched with the
    ``FNM_PATHNAME`` translation above, so ``*`` does not cross ``/``.
    """

    if pattern == "~ALL":
        return True
    return re.match(_fnm_pathname_regex(pattern), ref) is not None


def _dispatched_tag_ref(tag: str, *, tag_prefix: str) -> str:
    """Return the concrete ref the ruleset must cover for ``tag``.

    The gate checks the exact ref that will be written (``refs/tags/<tag>``) so
    a ruleset that covers only a subset of the versioned tags (for example
    ``refs/tags/v0.*`` or a ``v*`` include minus a ``v*-rc.*`` exclude) does not
    pass for a tag it does not actually protect. When no concrete tag is
    available (a best-effort report), the representative ``refs/tags/<prefix>``
    with no version suffix is used.
    """

    return f"refs/tags/{tag}" if tag else f"refs/tags/{tag_prefix}"


def tag_ruleset_covers_v_tags(client: GitHubClient, *, tag: str, tag_prefix: str = "v") -> bool:
    """Return True if an active tag ruleset actually covers ``refs/tags/<tag>``
    and restricts who may create, update, or delete it.

    Reads each tag-target ruleset's ``conditions.ref_name`` and ``rules`` with
    ``GET /repos/{o}/{r}/rulesets/{id}`` (``Metadata: read``). A ruleset counts
    only when it is ``active``; an include pattern matches ``refs/tags/<tag>``
    (``~ALL`` or an ``FNM_PATHNAME`` pattern) with no exclude pattern that
    removes it; and it carries at least one restricting rule type
    (``creation`` / ``update`` / ``deletion``), so matching alone — without any
    rule that gates the write — does not pass the gate.
    """

    ref = _dispatched_tag_ref(tag, tag_prefix=tag_prefix)
    for summary in client.list_rulesets(target="tag"):
        if str(summary.get("enforcement", "")) != "active":
            continue
        ruleset_id = summary.get("id")
        if not isinstance(ruleset_id, int):
            continue
        detail = client.get_ruleset(ruleset_id)
        if str(detail.get("enforcement", summary.get("enforcement", ""))) != "active":
            continue
        conditions = (detail.get("conditions") or {}).get("ref_name") or {}
        include = conditions.get("include") or []
        exclude = conditions.get("exclude") or []
        covers = any(_ruleset_pattern_matches_ref(str(pattern), ref) for pattern in include if isinstance(pattern, str))
        if not covers:
            continue
        if any(_ruleset_pattern_matches_ref(str(pattern), ref) for pattern in exclude if isinstance(pattern, str)):
            continue
        rule_types = {str(rule.get("type", "")) for rule in (detail.get("rules") or []) if isinstance(rule, dict)}
        if rule_types & _TAG_RESTRICTING_RULE_TYPES:
            return True
    return False


def validate_tag_ruleset_present(client: GitHubClient, *, tag: str, tag_prefix: str = "v") -> None:
    """Fail closed unless an active tag ruleset covers ``refs/tags/<tag>``.

    A non-dry run must not publish a tag the repository does not protect with a
    tag ruleset that both matches the dispatched ref and restricts who may
    create, update, or delete it. The ruleset's ref conditions and rule list are
    readable with ``Metadata: read``, so this is a machine check, not advice.
    """

    _require(
        tag_ruleset_covers_v_tags(client, tag=tag, tag_prefix=tag_prefix),
        f"no active tag ruleset covers refs/tags/{tag_prefix}* with a creation/update/deletion rule; "
        f"add a tag ruleset whose ref-name condition includes the {tag_prefix}* tags and restricts "
        "who may create, update, or delete them before releasing",
    )


def report_release_settings(client: GitHubClient, *, tag: str = "", tag_prefix: str = "v") -> dict[str, str]:
    """Best-effort, non-failing report of the two settings immutability depends on.

    Returns a small status mapping for the two repository settings the
    "immutable milestone" guarantee relies on:

    * ``immutable_releases``: GitHub's immutable-releases setting cannot be read
      with the workflow ``GITHUB_TOKEN`` (reading it needs repository-admin
      permission the token cannot be granted), so this is always
      ``"unverifiable-pre-run"``. It is instead enforced after publication by the
      read-back check, which fails the run unless the published release reports
      ``immutable: true``.
    * ``tag_ruleset``: whether an active ``tag``-target ruleset actually covers
      the dispatched ``refs/tags/<tag>`` (or, when no tag is supplied, the
      representative ``refs/tags/<prefix>``) and restricts who may create,
      update, or delete it. Each tag ruleset's ref-name conditions and rule list
      are read with ``Metadata: read`` (already granted), so a ruleset that
      covers and restricts the tag is reported ``"present"`` and one that does
      not (or no tag ruleset at all) is reported ``"absent"``.

    This never raises; a read error is reported as ``"unreadable"`` so a
    transient API failure cannot block a release here. The authoritative tag
    ruleset gate before a non-dry run is :func:`validate_tag_ruleset_present`,
    which checks the same dispatched ref.
    """

    report = {"immutable_releases": "unverifiable-pre-run", "tag_ruleset": "absent"}
    try:
        covered = tag_ruleset_covers_v_tags(client, tag=tag, tag_prefix=tag_prefix)
    except Exception:  # noqa: BLE001 - advisory read only; never block on it
        report["tag_ruleset"] = "unreadable"
        return report
    report["tag_ruleset"] = "present" if covered else "absent"
    return report


def report_environment_policy(client: GitHubClient, *, environment: str = RELEASE_ENVIRONMENT) -> dict[str, str]:
    """Best-effort, non-failing report of the two machine-checkable environment
    rules a dry run cannot otherwise surface.

    ``validate_environment_policy`` enforces prevent-self-review and the
    main-only deployment branch policy, but only in the publish step after
    approval. A dry run never reaches that step, so this read-only report lets
    ``verify-environment`` warn about either before approval. Returns a mapping
    with ``prevent_self_review`` and ``branch_policy`` each one of ``"ok"``,
    ``"missing"``, or ``"unreadable"``; it never raises.
    """

    report = {"prevent_self_review": "missing", "branch_policy": "missing"}
    try:
        payload = client.get_environment(environment) or {}
        rules = payload.get("protection_rules") or []
        report["prevent_self_review"] = (
            "ok"
            if any(
                isinstance(rule, dict)
                and str(rule.get("type", "")) == _REQUIRED_REVIEWERS_RULE
                and rule.get("prevent_self_review") is True
                for rule in rules
            )
            else "missing"
        )
        branch_policy = payload.get("deployment_branch_policy")
        names = sorted(str(p.get("name", "")) for p in client.list_deployment_branch_policies(environment))
        report["branch_policy"] = (
            "ok"
            if isinstance(branch_policy, dict)
            and branch_policy.get("custom_branch_policies") is True
            and names == [MAIN_BRANCH]
            else "missing"
        )
    except Exception:  # noqa: BLE001 - advisory read only; never block on it
        return {"prevent_self_review": "unreadable", "branch_policy": "unreadable"}
    return report


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
