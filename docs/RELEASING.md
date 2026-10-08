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
  missing. A dry run is otherwise not a full rehearsal of publication: the
  read-only validate/build jobs cannot list draft releases (only a token with
  push access can), so a draft that already holds the tag is detected only at
  publish time. Run a dry run, inspect the artifacts, then dispatch again with
  `dry_run: false`.

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
| `backend-sbom.spdx.json` | Backend SPDX SBOM from a pinned syft over `dir:backend` **without installing dependencies**, normalized (creation time set to the commit date in UTC `YYYY-MM-DDThh:mm:ssZ`, a namespace made unique per release from the source name, tag, and commit, and absolute paths removed). syft records only public package-registry hosts, so no hostname scrubbing is performed; the exclusion guard independently rejects a fixed list of AWS-owned host suffixes (the AWS service domain, the retail domain, and the AWS developer-tools domain) plus the internal-domain markers it lists, so a release carrying one of those fails closed rather than publishing it. |
| `frontend-sbom.spdx.json` | Frontend SPDX SBOM over `dir:ui`, normalized the same way. |
| `SHA256SUMS` | `sha256sum`-format checksums, sorted by filename, covering every other attached file. |

An exclusion guard rejects any artifact outside this allowlist and any content
containing ARNs, account-ID patterns, credentials/tokens, `.env` /
`.bedrock_agentcore` material, private registry or internal hostnames, or source
archives. **GitHub supplies the source `.zip`/`.tar.gz` automatically for the
tag; the workflow never attaches one.**

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

Every job also runs only when the dispatch ref is `main`; the workflow file
itself comes from the dispatch ref, so the commit-equals-`main` check in the
scripts is not sufficient on its own.

Validation runs entirely before any write. In the publish job, the ordering is:

1. download the verified artifact set and verify it against `SHA256SUMS` and the
   exclusion guard;
2. verify the manifest's tag, commit, and attached-file list equal the dispatch
   inputs (the downloaded bytes are never trusted to assert their own identity);
3. verify the `release` environment exists and is protected by a
   required-reviewers rule (fail closed), re-verify `main` HEAD still equals the
   commit and the tag ref is still absent, and require the downloaded manifest
   to be **byte-identical** to a manifest rebuilt from this checkout and the
   publish-time re-validation result (so an evidence edit during the approval
   wait, or manifest tampering in the build job, cannot be published);
4. install the SIGTERM-to-exception handler, then create a **draft** release
   with notes and upload the verified assets (draft creation sits inside the
   guarded block, so an ambiguous or interrupted creation is cleaned up by
   `tag_name`);
5. re-check `main` HEAD and tag-ref absence **immediately before** the tag
   write (closing the race between the earlier check and the write), then create
   the annotated tag object and the `refs/tags/<tag>` ref at the exact commit;
6. publish the draft (`prerelease` and non-latest for `-rc.N`; a stable tag is
   set `make_latest` explicitly).

The annotated tag object's **tagger date is the commit date**, not the
publication time, so re-running the publish for the same commit produces a
byte-reproducible tag object.

The publish step is dispatched as `exec python3 -m release.cli publish`, so the
runner replaces the `bash -e` step wrapper with Python. A cancelled step's
SIGINT and the follow-up SIGTERM then reach the publisher directly rather than
stopping at the shell. Publication HTTP calls use a finite timeout so a stalled
call cannot hold the run. Cleanup runs on any interruption (including a
cancelled step's SIGINT and the follow-up SIGTERM) and decides from the **live
remote state**, not local flags: the draft is deleted only while it is still a
draft, and the tag ref is deleted only while it still points at this run's tag
object and no published release uses the tag. Cleanup itself blocks SIGINT and
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
time to remove.

### Manual recovery

Cleanup prints to stderr, and the combination of lines tells you exactly what
state the run left behind. It always prints an **announcement** line first —
`release cleanup starting; possible leftovers to verify: ...` — naming every
identifier the run may have created (the tag, the tag-object SHA, and the draft
release id, whatever was known). After that one of three things happens, and
the runner sends **SIGKILL about 10 seconds** after the SIGINT that starts a
cancellation, so a kill can land before cleanup finishes:

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
2. If a **published** release exists for the tag, the release succeeded despite
   the error; confirm its assets and stop.
3. If only a **draft** release or an orphaned tag ref remains, delete the tag
   ref first (if it still points at the run's tag object), then delete the
   draft, then re-dispatch. With GitHub immutable releases enabled, a deleted
   release's tag name cannot be reused, so pick the next tag if the name was
   burned.

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

## Recommended maintainer settings

These harden releases further and should be configured in repository settings.
The release tooling intentionally does not change settings, rulesets, or
environments; several guarantees in this document depend on these being set:

- **GitHub immutable releases** for the repository, so published release assets
  and tags cannot be altered after the fact. The "immutable milestone" guarantee
  above holds only once this is enabled.
- A **`v*` tag ruleset** restricting who can create or update release tags and
  requiring them to be created by this workflow.
- The **`release` environment** (used by the publish job) with **required
  reviewers**, **prevent self-review** enabled, and **administrators not allowed
  to bypass** the protection rule, plus a **deployment branch rule limiting the
  environment to `main`**. Maintainers must create and protect this environment
  **before any run, dry or not** — the validate job's `verify-environment` step
  (also run again in the publish job before any write) fails closed if the
  environment is missing or has no required-reviewers rule, so a first dispatch
  can never publish into an auto-created, unprotected environment. The
  `verify-environment` check confirms a required-reviewers rule exists but does
  **not** enforce `prevent_self_review`; enable prevent-self-review and
  no-admin-bypass yourself so a reviewer cannot self-approve their own release.
