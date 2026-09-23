# Final independent review — E1 operations observation infra

Scope: E1 infra diff ending `ee86ba3` on `feat/413-e1-observation-infra`
(commits `ed45dba`, `6f526f6`, `ee86ba3`). No AWS was touched. All checks are
static/parser-based plus the repo's own unit tests, run in a local `uv` venv.
Authoritative contract source: sibling core worktree `logs/issue-413-core`
(`feat/413-e1-observation-core`, HEAD `e5346a5`).

## Verdict: PASS

`ee86ba3` resolves both findings from the prior re-review (frozen env-name
mismatch; infra-owned handler placeholder). Every acceptance item is verified.

---

## Verified

- **Frozen env names + params.** Template injects exactly the four frozen `_S`
  names core's `resolve_operations_settings()` reads —
  `GBAW_OPERATIONS_PER_READ_BUDGET_S`, `_PERSISTENCE_BUDGET_S`,
  `_CANCELLATION_MARGIN_S`, `_OBSERVATION_TTL_S` — each backed by a bounded
  `Number` parameter, plus the six identity/table bindings (`_MODE`,
  `_TABLE_NAME`, `_METRIC_NAMESPACE`, `_TENANT_ID`, `_WORKSPACE_ID`,
  `_TRUSTED_AUDIENCE`). The stale `_SECONDS`/`REQUEST_DEADLINE`/`PROVIDER_READ`/
  `RECORD_TTL`/`CONTENT_BUCKET` names are gone from template, wrapper, and docs.
  `RequestDeadlineSeconds` is kept ONLY as the Lambda `Timeout` (not injected),
  matching core deriving the deadline from sub-budgets + margin.
  `ObservationTtlSeconds` default is now 1800 s (30 min), matching core's
  default (prior 30-day contradiction resolved). Tests encode the contract:
  `REQUIRED_ENV_KEYS` = the `_S` names; `FORBIDDEN_ENV_KEYS` = the old names.
- **No handler source conflict.** The infra worktree deleted its
  `backend/src/operations/observe/` placeholder entirely; core owns the sole real
  module-level `handler(event, context)` at `operations.observe.lambda_entry`,
  which is exactly the template `Handler`. No add/add collision on merge.
- **Packaging dep pins vs uv.lock.** All six pins in `PINNED_DEPS` match
  `backend/uv.lock` exactly: rfc8785 0.1.4, jsonschema 4.26.0,
  jsonschema-specifications 2025.9.1, referencing 0.36.2, attrs 25.4.0,
  rpds-py 2026.5.1. Closure is complete for the operations package's third-party
  imports (rfc8785, jsonschema, referencing + transitives); boto3/botocore
  correctly excluded (Lambda-provided).
- **manylinux x86_64 correctness.** Installs binary-only for
  `manylinux2014_x86_64` / cp313 (uv `--python-platform x86_64-manylinux2014`
  or pip `--platform … --abi cp313 --only-binary=:all:`), with a build-image
  fallback. Fails closed (exit 5) if any `*.so` carries a macosx/arm64/aarch64/
  win/i686 tag, and if the native `rpds*.so` is absent.
- **Deterministic zip.** Sorted entries (`find … | LC_ALL=C sort | zip -X -@`),
  fixed epoch mtime (`touch -h -t`), `.dist-info`/`__pycache__` stripped;
  content-hash S3 key. Combined-package test asserts byte-stability.
- **Clean Linux import probe.** Imports the frozen handler inside
  `public.ecr.aws/lambda/python:3.13-x86_64` when Docker is available; otherwise
  a fail-closed structural probe requires the handler module + each required
  top-level dependency to be present.
- **Explicit profile/account/region + artifact-bucket checks.** `AWS_PROFILE`
  passed explicitly to every `aws` call in both wrappers (fallback `default`).
  Requires an explicit pre-existing `GBAW_OPERATIONS_ARTIFACT_BUCKET` (exit 6 if
  unset); no list-buckets discovery, no bucket creation. Verified with
  `head-bucket --expected-bucket-owner <caller account>` and a
  `get-bucket-location` region-match check (incl. us-east-1 `None`). Identity
  gate (`sts get-caller-identity`) before any write on both wrappers.
- **No-write preview + opt-in.** Default `preview` runs cfn-lint (if present) +
  `validate-template` only, then exits — no change set, no mutation. Enable
  requires BOTH `GBAW_OPERATIONS_MODE=observe` and `--enable`; refuses on missing
  issuer/client/tenant/workspace/bucket.
- **Template Rules.** `EnabledModeRequiresInputs` gated on `observe` asserts
  non-empty CognitoIssuer/ClientId/TenantId/WorkspaceId/CodeS3Bucket/CodeS3Key;
  disabled default asserts nothing (defaults deploy valid).
- **Default-zero resources.** All 18 resources carry `Condition:
  OperationsEnabled` (`OperationsMode` default `disabled`). Only the
  `OperationsMode` output is unconditional (echoes the param; no GetAtt). Stack
  unwired from deploy.sh/deploy-all.sh; teardown wrappers unwired from
  teardown-all.sh/teardown.sh.
- **Exact IAM/KMS/log auth.** GameLift: exactly the 3 reads core's adapter uses
  (DescribeFleetUtilization/FleetCapacity/ScalingPolicies). DynamoDB
  GetItem/Query/TransactWriteItems scoped to the table ARN. KMS Decrypt +
  GenerateDataKey only (no Encrypt), scoped to the CMK; CMK grants
  logs.<region>.amazonaws.com Decrypt+GenerateDataKey scoped by
  `kms:EncryptionContext:aws:logs:arn` to `/aws/*/game-agent-operations*`. Logs
  CreateLogStream/PutLogEvents to own log group; PutMetricData namespace-scoped;
  X-Ray write (matches TracingConfig Active). No S3, no iam:PassRole, no
  wildcard action, no GameLift/DynamoDB writes beyond the table transaction.
  Both log groups KMS-encrypted, retention 90 prod / 14 beta; table KMS SSE +
  PITR + TTL + on-demand caps + prod deletion protection; CMK/table Retain.
- **p99 alarm.** `ObservationRequestLatency` uses `ExtendedStatistic: p99`
  (not the invalid `Statistic: p99`), Threshold 27000 ms (ADR 0005 ceiling −
  margin), GreaterThanThreshold. Three Sum alarms (Failures/Timeouts/Stuck)
  present. Metric names + namespace match core.
- **Status route.** `GET /operations/{operationId}` with JWT authorizer;
  `POST /operations/observe` likewise. Core handler dispatches POST observe +
  GET status.
- **Teardown safety.** Requires `--confirm delete-operations`; deletes only the
  stack; DynamoDB table + KMS key retained (audit data preserved); no
  `--delete-data`; never auto-invoked.
- **No untracked scratch required.** All diff dependencies are committed at
  `ee86ba3`. Combined-package test builds fixtures in `tmp_path` and asserts the
  worktree ships no `operations/observe` placeholder. The only untracked file is
  this findings doc.
- **Tests.** 62/62 pass across `test_operations_observation_infra_unit`,
  `test_operations_combined_package_unit`,
  `test_operations_wrappers_behavior_unit`. Both wrappers pass `bash -n`.

(cfn-lint not installable in this sandbox; template validated structurally.)
