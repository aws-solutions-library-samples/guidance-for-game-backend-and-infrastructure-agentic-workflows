# Releasing

This repository publishes **immutable milestone releases**. A release records an
exact, accepted `main` source state as an annotated Git tag plus a GitHub
Release carrying only public-safe, deterministic artifacts. **A release never
deploys or activates that state**, and the release workflow never holds AWS
credentials.

- Workflow: [`.github/workflows/release.yml`](../.github/workflows/release.yml)
- Tooling: [`scripts/release/`](../scripts/release/)
- Tests: [`scripts/test/test_release_*.py`](../scripts/test/)

## Policy

- **Immutable annotated tags.** A published tag is never moved, replaced, or
  reused. The workflow refuses a tag that already exists as a ref, that is used
  by any existing release or draft, or that is not strictly greater (by semver
  precedence) than every existing `v*` tag — so a deleted or older name can
  never be reused to look newer.
- **Pre-1.0 series.** While the public compatibility and support contract is
  evolving, releases use `v0.x`. The first stable read-only baseline is
  `v0.1.0`.
- **Tag grammar.** `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH-rc.N`. No leading
  zeros, no build metadata, no whitespace, no alternative prerelease
  identifiers. `-rc.N` tags are published as prereleases.
- **Manual dispatch only.** The workflow runs on `workflow_dispatch`. Creating a
  tag or release never triggers an AWS deployment.
- **Dry run first.** `dry_run` defaults to `true`. A dry run performs the
  pre-write validations and the artifact build and uploads the artifacts as a
  workflow artifact, but never touches tags or releases. The validate job runs
  the environment-protection check, so the `release` environment must exist and
  be protected **before any run, dry or not** — a dry run fails closed if it is
  missing. The validate job's `verify-environment` step also **reports** whether
  GitHub's immutable-releases setting can be confirmed (it cannot be read ahead
  of a run with the workflow token, so it is enforced after publish instead),
  whether an active `v*` tag ruleset covers the versioned tags and restricts who
  may create, update, or delete them, and whether the environment prevents
  self-review and limits deployments to a `main`-only branch policy (the latter
  two are enforced in the publish step after approval, so the dry-run report is
  the only pre-approval warning of a weak policy). A dry run is otherwise not a
  full rehearsal of publication: the read-only validate/build jobs cannot list
  draft releases (only a token with push access can), so a draft that already
  holds the tag is detected only at publish time. Run a dry run, inspect the
  artifacts, then dispatch again with `dry_run: false`.

## Dispatch inputs

| Input | Meaning |
| --- | --- |
| `commit` | Full 40-hex SHA. Must equal current `main` HEAD. |
| `tag` | The release tag (grammar above). |
| `evidence_comment_url` | Permalink of the deployed-validation evidence comment on an issue **in this repository**. |
| `dry_run` | `true` (default) validates and builds artifacts only; `false` publishes. |

## Evidence comment format

A maintainer records deployed-validation evidence as a comment on an issue in
this repository. The comment author must have **write permission or higher** on
this repository (confirmed through the collaborator-permission endpoint, not the
broad `author_association` field). The comment must contain **exactly one**
fenced ` ```json ` block matching this strict, versioned schema (unknown keys
are rejected):

```json
{
  "schema_version": "gbaw.release-evidence.v1",
  "commit": "<the exact 40-hex commit being released>",
  "gates": {
    "tag_points_at_main_and_checks_green": "pass",
    "p0_p1_closed_or_waived": "pass",
    "exact_commit_deployed_to_isolated_nonprod": "pass",
    "authenticated_shakedowns_pass": "pass",
    "unauthorized_requests_denied": "pass",
    "kb_ingestion_zero_failures": "pass",
    "frontend_runtime_waf_alarms_vuln_ok": "pass",
    "public_content_scan_passes": "pass",
    "operations_mode_disabled_default_no_write": "pass",
    "rollback_documented_and_tested": "pass"
  },
  "waivers": {}
}
```

Every gate must be `pass` or `waived`. **Only `p0_p1_closed_or_waived` may be
`waived`;** every other gate encodes a hard release invariant and must be an
affirmative `pass`. A `waived` gate requires a public issue reference in
`waivers`, e.g. `"p0_p1_closed_or_waived": "waived"` with
`"waivers": { "p0_p1_closed_or_waived": "#412" }`. Duplicate JSON keys are
rejected. The comment is run through the repository public-content scanner, the
`commit` must equal the dispatched commit, the permalink's issue number must
match the comment's issue, and the comment author must have **write permission
or higher** on this repository (not merely be an org member or read-only
collaborator). These gates correspond to the release gate in the parent release
issue.

## Required checks

Publication requires that every required status check on `main` has a
completed, successful **latest** check run on the exact release commit, produced
by the GitHub Actions app. The required contexts are **read live** from the
`main` branch ruleset (`GET /repos/{owner}/{repo}/rules/branches/main`, Metadata
read) at validation time, so a renamed or newly added required check is enforced
automatically rather than from a hardcoded list. Each context's runs are read
across **all pages** (`per_page=100`), ordered by `started_at` (then
`completed_at`, then `id`); runs with a `skipped` conclusion (which a scheduled
workflow run records on the same commit) are ignored when choosing the latest
run. The newest non-skipped run must be **completed and successful**, so neither
many newer scheduled skips nor a newer, still-in-progress rerun of a required
check can let an older success stand in for the current state. A run must carry
a present `head_sha` equal to the release commit and a present `app.id` matching
the required integration; a missing field fails closed.

Non-required runs (for example, Dependabot) are ignored.

## Release notes

Each release requires `docs/releases/<tag>.md` at the release commit, containing
these sections: **defaults**, **capabilities**, **known issues**,
**compatibility**, **deployment**, **rollback**. Matching is a case-insensitive
substring check over the whole file (not a heading-structure check), so each
required word must appear somewhere in the notes. The file is scanned for
public-content violations. See
[`docs/releases/TEMPLATE.md`](releases/TEMPLATE.md).

## Attached artifacts and exclusions

The workflow attaches **only** these four files, generated deterministically
from the exact tag:

| Artifact | Contents |
| --- | --- |
| `release-manifest.json` | Schema version, tag/name, commit and commit date, default operations mode (proven `disabled` by a fail-closed scan of **every tracked file**, regardless of suffix: the ADR declares the default and no tracked file references `GBAW_OPERATIONS_MODE` except Markdown files and files under `docs/`, `scripts/release/`, and `scripts/test/`), SHA-256 of dependency locks, managed-prompt sources, and every `*.yaml`/`*.yml` under `infrastructure/` (both CloudFormation templates and the Kubernetes RBAC manifests, under `hashes.infrastructure_templates`), required-check conclusions, the evidence digest, gate summary, and waiver issue references, and the attached file names. Serialized with sorted keys, two-space indent, ASCII, LF, trailing newline. |
| `backend-sbom.spdx.json` | Backend SPDX SBOM from a pinned syft over `dir:backend` **without installing dependencies**, normalized (creation time set to the commit date in UTC `YYYY-MM-DDThh:mm:ssZ`, a namespace made unique per release from the source name, tag, and commit, and absolute paths removed). syft records only public package-registry hosts, so no hostname scrubbing is performed; the exclusion guard independently rejects any hostname that is not an allowlisted public package-registry host (reporting an AWS/Amazon-owned suffix distinctly), so a release carrying an internal, private, or otherwise unexpected host fails closed rather than publishing it. |
| `frontend-sbom.spdx.json` | Frontend SPDX SBOM over `dir:ui`, normalized the same way. |
| `SHA256SUMS` | `sha256sum`-format checksums, sorted by filename, covering every other attached file. |

An exclusion guard rejects any artifact outside this allowlist and any content
containing ARNs, account-ID patterns, credentials/tokens, `.env` /
`.bedrock_agentcore` material, source archives, or any hostname that is not an
allowlisted public package-registry host — so an internal or unknown private
registry host (for example one carried in a lockfile index URL) fails closed
rather than being published. **GitHub supplies the source `.zip`/`.tar.gz`
automatically for the tag; the workflow never attaches one.**

## Failure behavior

Artifact generation and publication are split across two jobs with different
privilege:

- **`build-artifacts`** holds a **read-only** token. It runs syft twice over the
  source, builds the artifact set from each run, and fails unless the two builds
  are **byte-identical**. Only the verified set is uploaded as a workflow
  artifact.
- **`publish`** holds `contents: write` and uses only first-party `actions/*`
  actions plus the stdlib tooling — no syft and no SBOM regeneration run beside
  the write token. It downloads the verified set and never rebuilds it.

A dedicated `guard-ref` job runs on every dispatch with no ref condition, so a
dispatch from a non-`main` ref fails loudly there instead of skipping every
gated job and showing green. Every other job also runs only when the dispatch
ref is `main` and depends on `guard-ref`; the workflow file itself comes from
the dispatch ref, so the commit-equals-`main` check in the scripts is not
sufficient on its own.

Validation runs entirely before any write. In the publish job, the ordering is:

1. download the verified artifact set and verify it against `SHA256SUMS` and the
   exclusion guard;
2. verify the manifest's tag, commit, and attached-file list equal the dispatch
   inputs (the downloaded bytes are never trusted to assert their own identity);
3. verify the `release` environment exists and its policy is fully
   machine-confirmable — a required-reviewers rule, **prevent-self-review**, and
   a deployment branch policy limited to **`main`** (fail closed) — and verify
   that an **active `v*` tag ruleset** covers the dispatched `refs/tags/<tag>`
   and carries a `creation`/`update`/`deletion` rule (fail closed); re-verify
   `main` HEAD still equals the commit and the tag ref is still absent, and
   require the downloaded manifest to be **byte-identical** to a manifest
   rebuilt from this checkout and the publish-time re-validation result (so an
   evidence edit during the approval wait, or manifest tampering in the build
   job, cannot be published);
4. install the SIGTERM-to-exception handler, then create a **draft** release
   with notes and upload the verified assets (draft creation sits inside the
   guarded block, so an ambiguous or interrupted creation is cleaned up by
   `tag_name`);
5. verify the **draft's** attached assets (names, sizes, and digests where
   GitHub reports them) **before** creating the tag, so a draft whose attached
   assets do not match the published set at that point is caught while the draft
   can still be cleaned up and the tag name is not yet burned;
6. re-check `main` HEAD and tag-ref absence **immediately before** the tag
   write (closing the race between the earlier check and the write), then create
   the annotated tag object and the `refs/tags/<tag>` ref at the exact commit;
7. publish the draft (`prerelease` and non-latest for `-rc.N`; a stable tag is
   set `make_latest` explicitly);
8. read the published release back and fail the run unless every post-publish
   check passes: the release reads back as a published, **immutable** release
   (`immutable: true`), exactly the four allowlisted assets are attached with a
   size equal to the published bytes and, when GitHub reports an asset
   `digest`, a sha256 equal to its `SHA256SUMS` entry, and the tag ref resolves
   through this run's annotated tag object to the released commit. Every check
   runs even when an earlier one fails, so the run reports all the failures at
   once rather than only the first.

The annotated tag object's **tagger date is the commit date**, not the
publication time, so re-running the publish for the same commit produces a
byte-reproducible tag object.

The publish step is dispatched as `exec python3 -m release.cli publish`, so the
runner replaces the `bash -e` step wrapper with Python. A cancelled step's
SIGINT and the follow-up SIGTERM then reach the publisher directly rather than
stopping at the shell. Publication HTTP calls use a finite timeout so a stalled
call cannot hold the run. Cleanup runs on any interruption (including a
cancelled step's SIGINT and the follow-up SIGTERM). When the publish PATCH
already returned success, cleanup deletes **nothing** — a published release and
its locked tag are left intact for manual verification (it decides this from the
observed PATCH outcome, so a stale live read cannot send it down a delete path).
Otherwise it re-reads the **live remote state**: the draft is deleted only while
it is still a draft, and the tag ref is deleted only while it still points at
this run's tag object and no published release uses the tag. Cleanup itself
blocks SIGINT and
SIGTERM for its duration so the follow-up SIGTERM cannot abort it halfway.
The runner sends SIGKILL about **10 seconds** after the SIGINT that starts a
cancellation. Cleanup chooses its budget by **what interrupted it**. On the
**cancel path** (a cancelled step's SIGINT or the follow-up SIGTERM) it runs
under an **8-second total budget** with a **2-second per-request timeout**, so
it finishes inside that kill window **when the cancellation's SIGINT starts
cleanup**. If the follow-up SIGTERM is what starts cleanup (which happens only
when the inherited SIGINT did not raise), only about **2.5 seconds** remain
before SIGKILL, so the runner may kill cleanup partway through; the
announcement-only outcome below then applies. On an **ordinary failure** (an API
error with no incoming kill) it runs under a longer **120-second total /
30-second per-request** budget — matching the HTTP client's own socket timeout —
so a slow-but-responsive API still has every leftover removed instead of being
abandoned inside the cancel window. No job sets `timeout-minutes`, so the
**default 360-minute** job timeout bounds the publish job, leaving ample room
for the ordinary budget. Either way, cleanup prints the possible
leftovers (the tag name, the tag-object SHA, and the draft release id, whatever
is known) **before its first request** — so even if the kill lands mid-cleanup
an operator has the exact identifiers to check. It deletes the **tag ref first**
(a public tag with no release is the worst thing to leave) and the draft release
second. When everything announced is confirmed removed it prints a positive
completion line; anything not confirmed removed within the budget is printed for
manual recovery. The ordinary budget removes the tag ref and draft **only when
the API answers within the budget**; against a dead or non-responsive API, both
are reported as left intact for manual recovery. A client-visible error does not
prove the server rejected the write, so a release whose publish PATCH was
applied server-side and then answered with an error is reported as a **published
release left intact** rather than deleted — cleanup never deletes a published
release. On the cancel path, cleanup never leaves a stable-looking tag it had
time to remove. (One caveat: if any ruleset covering `refs/tags/<tag>` carries
a `deletion` rule and this workflow's identity is not a bypass actor, deleting
`refs/tags/<tag>` is itself refused, so cleanup reports the ref as left intact
for manual recovery. Restrict release tags with an `update` rule only, which
leaves this workflow's create and cleanup writes alone, so cleanup can remove
the ref — see "Recommended maintainer settings".)

### Manual recovery

Cleanup prints to stderr, and the combination of lines tells you exactly what
state the run left behind. Its first line is an **announcement**. On the
failure/interrupt paths (the publish PATCH did not return success) that line is
`release cleanup starting; possible leftovers to verify: ...`, naming every
identifier the run may have created (the tag, the tag-object SHA, and the draft
release id, whatever was known). When the publish PATCH **did** return success,
cleanup deletes nothing and the first line instead reads `release cleanup
starting; the release was published, so nothing is deleted. Published release
left intact for manual verification: ...`. After the announcement one of three
things happens, and the runner sends **SIGKILL about 10 seconds** after the
SIGINT that starts a cancellation, so a kill can land before cleanup finishes:

- **Announcement + `release cleanup complete; nothing left to remove`.**
  Everything announced was confirmed removed. There is nothing to do.
- **Announcement + `release cleanup could not remove everything; manual
  recovery required for: ...`.** Only the items named on the manual-recovery
  line remain (for example a tag ref or draft whose state could not be
  confirmed, or whose removal the budget ran out of time for). Recover only
  those.
- **Announcement alone, with neither follow-up line.** The ~10-second SIGKILL
  window elapsed and the process was killed mid-cleanup, so cleanup could not
  report an outcome. Treat **every** announced identifier as unverified and
  check each by hand.

To recover whatever remains:

1. Check whether `refs/tags/<tag>` exists and whether a release uses `<tag>`;
   the announcement line names the exact tag, tag-object SHA, and draft release
   id to look for.
2. If a **published** release exists for the tag, the publish PATCH succeeded.
   A run that exited without a `post-publish read-back` error is NOT, on its
   own, proof the release met the guarantee: a PATCH that GitHub applied but
   answered with an error, and a run cancelled during the read-back, both exit
   without that error and without a completed read-back, and in the first case
   the release can still be **mutable**. (A read-back whose reads kept failing
   does fail with a `post-publish read-back` error — the failed read is
   collected as an entry — so it is covered by the last paragraph of this step,
   not here.) So confirm it by hand rather than trusting the absence of an
   error:
   - `immutable: true` on the release (`gh release view <tag> --json` or the
     REST `GET /releases/{id}`);
   - the four attached assets each match their `SHA256SUMS` entry (download them
     and re-check, or compare each asset's reported digest);
   - `refs/tags/<tag>` resolves through this run's annotated **tag object** to
     the released commit (`git ls-remote`, then the REST `GET /git/tags/{sha}`).

   If every hand check passes, the release is good; keep it and stop. If any
   fails, follow **"A read-back failed after publication"** below. If the run
   **did** fail with a `post-publish read-back: ...` error (the release left
   intact), go straight to that section — it names every check that failed.
3. If only a **draft** release or an orphaned tag ref remains, delete the tag
   ref first (if it still points at the run's tag object), then delete the
   draft, then re-dispatch. If any ruleset covering `refs/tags/<tag>` carries a
   `deletion` rule and your identity is not a bypass actor, deleting the ref is
   refused, so bypass the ruleset (or temporarily relax it) to remove the
   orphaned ref.
   With GitHub immutable releases enabled, a deleted release's tag name cannot
   be reused, so pick the next tag if the name was
   burned.

#### A read-back failed after publication

The publish job reads the release back and fails the run if the release is not
immutable, if an asset or the tag does not match, or if the tag ref does not
resolve through this run's annotated tag object to the released commit. **Every
check runs even when an earlier one fails**, so the error lists all the failures
at once (for example "not immutable" and an asset mismatch together), not just
the first.

**First, re-run the checks by hand.** A read-back can fail on a stale read of a
release that is actually fine — a `release is still a draft` or a `tag_name`
mismatch read back moments after the publish PATCH, or a list that had not yet
caught up. Repeat the three hand checks from step 2 above (`immutable: true`,
the four assets against `SHA256SUMS`, and `refs/tags/<tag>` through the run's
tag object to the commit). **If they all pass, the release is good — keep it and
stop.** Only if a check genuinely fails do the choices below apply.

The release is **already published** and cleanup never deletes it, so a
re-dispatch of the same tag is refused (the tag ref now exists) and GitHub will
not make an existing release immutable after the fact (the setting applies only
to future releases). The real choices depend on which check genuinely failed:

- **The release is not immutable (nothing is locked yet).** Because the
  immutable-releases setting was off when it was published, the release and its
  tag are still mutable. Either:
  - keep it as a known mutable release (acceptable only if you accept a mutable
    milestone); or
  - delete the release and then `refs/tags/<tag>` (both are deletable while the
    setting is off), enable GitHub's immutable-releases setting, and dispatch the
    **same** tag again. Two constraints apply to this path:
    - **Deleting the tag ref may require a ruleset bypass.** The required `v*`
      tag ruleset blocks deleting `refs/tags/<tag>` when it carries a `deletion`
      rule, so you may need to bypass that ruleset (or temporarily relax it) to
      delete the ref.
    - **Re-dispatching the same tag needs the commit to still be `main` HEAD.**
      The workflow refuses unless the dispatched commit still equals `main`
      HEAD; if `main` has moved, re-dispatch against the new head with fresh
      evidence instead.
    Because nothing was immutable, the tag name can be reused.
- **The release is immutable but an asset or tag check failed.** The assets and
  tag are now locked, so the release **cannot be repaired**. Delete it if you do
  not want it (deleting an immutable release is allowed; the tag name is then
  burned and cannot be reused), and cut the **next** tag, which needs a new
  commit carrying `docs/releases/<next>.md` and fresh evidence.

## Rollback

Rollback here means **returning the source and deployment to a previously tagged
baseline**. **No emergency-disable mechanism is deployed by default** in this
repository, so today the only rollback lever is redeploying a known-good tagged
source.

To roll back:

1. Identify the last-good release tag (for example, the previous `v0.1.x`).
2. Check out that tag's source: `git checkout <tag>`.
3. Redeploy from that exact source using the normal deployment workflow
   (`scripts/deploy.sh`). The release workflow itself does not deploy.
4. Record the rollback and the reason on the relevant tracking issue.

When GitHub immutable releases are enabled (see recommended settings below), the
baseline you roll back to is byte-identical to what was validated when it was
published; `release-manifest.json` for that tag lets you confirm the
dependency-lock, prompt-source, and infrastructure-template hashes.

## Immutability: what the workflow enforces

The "immutable milestone" guarantee depends on two repository settings that the
release tooling deliberately does not change: GitHub's **immutable-releases**
setting and a **`v*` tag ruleset**. What the workflow can and cannot do about
them, with the existing `GITHUB_TOKEN`, is:

- **Immutable-releases setting (enforced after publish).** The setting itself
  cannot be read ahead of a run with the workflow token — reading it needs
  repository-admin permission that `GITHUB_TOKEN` cannot be granted, and the
  project adds no personal access token or extra permission for it. Instead, the
  publish job follows GitHub's recommended draft-then-publish order and then
  **reads the release back**: a published release reports `immutable: true` only
  when the setting is enabled, so the read-back **fails the run with a remedy**
  if it is not. This is positive, after-the-fact enforcement, not a pre-flight
  gate; a release can be created and will then fail its own read-back if the
  setting is off. GitHub does not mark the release in any way — only the run
  fails — so the published release is left intact for an operator to review and
  recover (see "A read-back failed after publication" under Manual recovery).
  Per GitHub's immutable-releases documentation, once an immutable release is
  published its associated Git tag is locked to the commit and cannot be moved
  or deleted while the release exists (deleting the release lets you delete the
  tag, but its name cannot be reused), and the release assets cannot be modified
  or deleted; only the title, notes, and the pre-release / latest flags stay
  editable. Creating an immutable release is what makes GitHub generate a
  release attestation automatically.
- **`v*` tag ruleset (reported in a dry run, enforced before a non-dry run).**
  The `verify-environment` step reads the repository's tag-target rulesets and
  each one's ref-name conditions and rule list (`GET /repos/{owner}/{repo}/rulesets`
  then `GET /repos/{owner}/{repo}/rulesets/{id}`, both of which need only
  `Metadata: read`) and reports whether an active ruleset covers the dispatched
  `refs/tags/<tag>` and restricts who may create, update, or delete it. GitHub
  aggregates the rules of **every** ruleset that covers a tag (see GitHub's
  "About rule layering"), so the report and the gate look at all covering
  rulesets, not just the first. Coverage is decided with Ruby's `File.fnmatch`
  and `File::FNM_PATHNAME` — the same matcher GitHub documents — so a `*` never
  crosses a `/` (`refs/*` does not cover a two-segment tag ref), a `**/` matches
  zero or more directories only as a whole path segment, and a bracket set never
  matches `/`. It is evaluated against the exact ref being written, not a single
  representative tag, so a ruleset that covers only part of the versioned tags
  (for example `refs/tags/v0.*`, or `v*` minus a `v*-rc.*` exclude) does not pass
  for a tag it does not protect. GitHub does **not** support a backslash as a
  quoting character or a `[^...]` complement in ruleset patterns; the matcher
  treats a backslash **outside** a bracket set as an escape of the next
  character and honors `[^...]` as a complement, which covers GitHub's supported
  patterns (those use neither). A matching ruleset counts only when it also
  carries at least one `creation`, `update`, or `deletion` rule.

  Which rule type to use matters, because the publish job writes the tag ref
  itself and deletes it during cleanup. Per GitHub's "Available rules for
  rulesets": **`creation`** lets only bypass actors create a matching tag,
  **`deletion`** (GitHub's default) lets only bypass actors delete one, and
  **`update`** lets only bypass actors push to (move) one. So an **`update`**
  rule is the one that leaves this workflow's own writes alone — it never moves
  an existing tag — while a `creation` rule would block the workflow's tag-ref
  write and a `deletion` rule would block cleanup's tag-ref delete. GitHub
  documents repository admins, organization and enterprise owners, the
  maintain/write role, teams, GitHub Apps, and Dependabot as eligible bypass
  actors; it does not document the GitHub Actions identity behind `GITHUB_TOKEN`
  as one, so **an `update` rule is the setup that works with the token this
  workflow already has**. The single-ruleset GET returns `current_user_can_bypass`
  for the calling token (`always` and `exempt` bypass the rules for a direct
  write; `pull_requests_only` bypasses only on a pull request, which a direct
  tag-ref write is not; `never` and an absent field are not a bypass). Before a
  non-dry publish the gate **fails closed** when any covering ruleset carries
  `creation` or `deletion` and the workflow token is not a bypass actor for it,
  with a remedy that points at the `update` rule. Otherwise the gate fails
  closed only when no active tag ruleset covers the dispatched tag ref with a
  restricting rule. A dry run reports `present`, `absent`, or `would-block`
  (a covering `creation`/`deletion` rule the token cannot bypass), so the
  bypass condition is surfaced before approval.

Options considered for the setting check were: (a) a documentation-only
dependency; (b) a pre-run read of the setting (not possible with the workflow
token); (c) the post-publish read-back of the `immutable` flag. The workflow
implements **(c)**, the strongest check available without adding a secret or a
new token permission, together with the machine-checked tag-ruleset gate.

## Before-first-run checklist

Confirm all of the following in repository settings **before the first
dispatch, dry or not**. Most are machine-checked by the workflow; the
administrator-bypass rule remains a hand check:

- [ ] **`release` environment exists** and has a **required-reviewers** rule
      with at least one reviewer. (Machine-checked: `verify-environment` fails
      closed without it.)
- [ ] **Prevent self-review** enabled on the `release` environment, so the
      dispatcher cannot approve their own release. (Machine-checked before a
      non-dry run: the environment payload's `prevent_self_review` is read with
      `actions: read` and the publish fails closed if it is off.)
- [ ] **Administrators cannot bypass** the `release` environment protection
      rule. (Not machine-checked — the environment payload has no documented
      admin-bypass field; confirm by hand.)
- [ ] **Deployment branch rule** limits the `release` environment to `main`.
      (Machine-checked before a non-dry run: the deployment branch policies are
      read with `actions: read` and must be exactly `main`.)
- [ ] **GitHub immutable-releases setting enabled** for the repository.
      (Enforced after publish by the read-back, which fails the run unless the
      published release is `immutable`.)
- [ ] **`v*` tag ruleset** that both covers the versioned tags and restricts who
      can create, update, or delete release tags. Use an **`update`** rule,
      which leaves this workflow's own tag creation and cleanup deletion alone —
      it is the setup that works with the token this workflow already has.
      (Machine-checked before a non-dry run: every active tag ruleset covering
      the dispatched `refs/tags/<tag>` is aggregated, at least one must carry a
      `creation`/`update`/`deletion` rule, and the run fails closed if any
      covering ruleset carries a `creation`/`deletion` rule the token cannot
      bypass; reported `present`/`absent`/`would-block` in a dry run.)

## Build provenance and attestation

Build-provenance attestation is **deferred**, consistent with the
no-AWS-credentials design. The workflow holds no `id-token` or `attestations`
permission and signs nothing itself; the attached SBOMs and the `SHA256SUMS`
manifest are the stated supply-chain boundary. When the immutable-releases
setting is enabled, creating an immutable release makes GitHub generate a
release attestation (a cryptographically verifiable record of the tag, commit
SHA, and assets), so enabling that setting is also what provides attestation
today. A dedicated, workflow-signed provenance attestation is tracked as a
follow-up.

## Recommended maintainer settings

These harden releases and, for the environment and the tag ruleset, are
**required before a non-dry publish** (the workflow fails closed without them).
The release tooling intentionally does not change settings, rulesets, or
environments; the guarantees in this document depend on these being set:

- **GitHub immutable releases** for the repository, so published release assets
  and tags cannot be altered after the fact. The "immutable milestone" guarantee
  above holds only once this is enabled.
- A **`v*` tag ruleset** restricting who can move release tags. Use an
  **`update`** rule: it restricts pushing (moving) an existing tag, which this
  workflow never does, so it leaves the workflow's own tag creation and cleanup
  deletion alone, and it is the setup that works with the token this workflow
  already has. A **`creation`** rule (restricting who may create a matching
  tag) or a **`deletion`** rule (restricting who may delete one, GitHub's
  default) would block this workflow's own writes — it creates `refs/tags/<tag>`
  and deletes it during cleanup. The publish job fails closed before the draft
  if any covering ruleset carries `creation` or `deletion` and its
  `current_user_can_bypass` is not a bypass value for the workflow token. GitHub
  does not document the Actions identity behind `GITHUB_TOKEN` as an eligible
  bypass actor, so that value is normally not a bypass and an `update` rule is
  the setup that works with the token this workflow already has. GitHub
  aggregates every covering ruleset, so a second covering `creation`/`deletion`
  ruleset (for example an organization or enterprise `~ALL` ruleset) is caught
  too; the refusal names the blocking ruleset so a maintainer can exclude the
  release tags from it or ask its owner when it is one they cannot edit.
- The **`release` environment** (used by the publish job) with **required
  reviewers**, **prevent self-review** enabled, and **administrators not allowed
  to bypass** the protection rule, plus a **deployment branch rule limiting the
  environment to `main`**. Maintainers must create and protect this environment
  **before any run, dry or not** — the validate job's `verify-environment` step
  (also run again in the publish job before any write) fails closed if the
  environment is missing or has no required-reviewers rule, so a first dispatch
  can never publish into an auto-created, unprotected environment. Before a
  non-dry publish, the publish job additionally fails closed unless the
  environment prevents self-review and its deployment branch policy is limited
  to `main` (both read with `actions: read`). Only admin-bypass has no
  documented field to read, so enable "administrators cannot bypass" by hand.
