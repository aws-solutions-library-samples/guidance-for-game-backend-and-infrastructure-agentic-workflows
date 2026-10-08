# vMAJOR.MINOR.PATCH

Copy this file to `docs/releases/<tag>.md` (for example `docs/releases/v0.1.0.md`)
and fill in each section before dispatching the release workflow. The workflow
requires all six section words below. Matching is a **case-insensitive substring
check over the whole file** (not a heading-structure check), so each required
word (`defaults`, `capabilities`, `known issues`, `compatibility`,
`deployment`, `rollback`) must appear somewhere in the notes. The file is also
run through the public-content scanner.

Write only reviewed public facts. Use synthetic identifiers; never include
account IDs, ARNs, endpoints, or internal hostnames.

## Defaults

What the default deployment creates and, importantly, what it does not. State
that `GBAW_OPERATIONS_MODE=disabled` is the default and that the chat runtime is
provider-read-only with no provider-write permissions.

## Capabilities

The read-only capabilities available in this release (GameLift, EKS, Cost chat
flows) and any new or changed behavior since the previous tag.

## Known issues

Open, release-relevant issues with public issue references. Call out any gate
that was waived for this release and link its waiver issue.

## Compatibility

The public compatibility and support contract for this pre-1.0 series: supported
regions, models, and any breaking changes relative to the previous tag.

## Deployment

How to deploy this exact tagged source (`scripts/deploy.sh`). Note that creating
the tag or release does not deploy anything.

## Rollback

How to roll back to this tag as a baseline (check out the tag and redeploy with
`scripts/deploy.sh`). Note that there is no deployed emergency-disable mechanism
yet — an operations kill switch is only planned — so rollback today means
redeploying a known-good tagged source. See [`docs/RELEASING.md`](../RELEASING.md).
