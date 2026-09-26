# Provider Response Inventory (model-visible surfaces)

Part of #457. This is a public-safe inventory of the provider responses that
can currently reach model context in the default read-only chat path: SDK
(boto3) tools, MCP tools, Bedrock Knowledge Base retrieval, and owned
deterministic tools. It records, per operation, the pagination/bounds, the
sensitive or customer-controlled fields the raw response can carry, the
transform applied before the model sees it, the error/partial semantics, and a
disposition decision. Surfaces still returning raw provider data are called out
explicitly as follow-ups rather than omitted.

Only synthetic identifiers are used in this document. It contains no account
IDs, ARNs, or deployment-specific coordinates.

## Disposition vocabulary

Code-owned projections classify each result into one mutually distinct
`status`:

| status | meaning |
| --- | --- |
| `ok` | full result set returned |
| `empty` | call succeeded, zero items |
| `denied` | provider refused (authorization/access) |
| `incomplete` | call failed, or only part of the set was retrieved (residual `NextToken`, no client-side cap) |
| `truncated` | result set bounded by the projection's item cap |

Typed, sanitized error codes (no provider text): `access_denied`,
`not_found`, `throttled`, `invalid_request`, `provider_error`,
`malformed_response` (the response shape itself was unusable — collection
missing/wrongly typed, or every item non-projectable).

## GameLift specialist (boto3, code-owned) — MIGRATED in this wave

| Operation | Pagination / bounds | Sensitive / customer-controlled fields in raw response | Transform | Error / partial semantics | Disposition |
| --- | --- | --- | --- | --- | --- |
| `list_gamelift_fleets` (`list_fleets` + `describe_fleet_attributes` + container APIs) | Paginates all fleet IDs; chunks describe at 100; **neither classic nor container fleet output is item-bounded to the model** — both paths append every page/summary with no aggregate cap | `FleetArn` (account ID), `FleetId`, `LogPaths`, `MetricGroups`, `ScriptId`/`BuildId`, `InstanceRoleArn`, location detail, arbitrary future attributes | **Container fleets:** code-owned `_summarize_container_fleet` allowlist. **Classic fleets: NONE** — raw `describe_fleet_attributes` items are appended unchanged and exposed under `FleetAttributes` (and `ClassicFleets`); `Warnings[]` carry raw provider `str(e)` text | Per-source `Warnings` (raw provider text); `error` only when totally empty | **Partially projected.** Container path is field-allowlisted but **not item- or value-capped**; the classic path returns **raw `FleetAttributes` including `FleetArn`/account ID and raw provider warning text** and is **also not item-capped**, and neither path is routed through the sanitized error vocabulary — **follow-up** to project the classic path, cap both paths, and sanitize warnings. |
| `get_fleet_utilization` (`describe_fleet_utilization`) | Single call; item cap `GAMELIFT_MAX_PROJECTED_ITEMS=100` + aggregate `GAMELIFT_MAX_PROJECTED_CHARS` budget measured against the **actual serialized JSON** (incl. escaping); residual `NextToken` (incl. empty page) → partial | `FleetArn` (account ID), arbitrary/future item fields, nested blobs | `project_fleet_utilization` allowlist + **field-specific grammar validators** (GameLift identifier/region/enum/name grammars; rejects control chars, ARN prefixes, account-ID patterns, URL schemes, IP/network coordinates, and malformed values even when short; finite bounded numbers); drops `FleetArn`, unknown fields, and allowed fields carrying an invalid value | Typed sanitized error; `NextToken` → `incomplete`; item/char cap or skipped non-dict items → `truncated`; empty+token → `incomplete`; malformed shape (missing/wrong collection type, non-dict items, all-invalid rows) → `incomplete` w/ `malformed_response` | **Migrated.** ok/empty/denied/incomplete/truncated distinct; not_found and malformed pinned via `error.code`. |
| `get_fleet_capacity` (`describe_fleet_capacity`) | Single call; item cap 100 + serialized-JSON char budget | `FleetArn`, `ManagedCapacityConfiguration`, arbitrary/future fields | `project_fleet_capacity` allowlist + field-specific grammar validators; `InstanceType`, `Location`, and bounded/validated `InstanceCounts` / `GameServerContainerGroupCounts` numbers | Typed sanitized error; empty+token → `incomplete`; malformed shape → `incomplete` w/ `malformed_response` | **Migrated.** |
| `get_scaling_policies` (`describe_scaling_policies`) | Single call; item cap 100 + serialized-JSON char budget | `FleetArn`, arbitrary/future fields, endpoints | `project_scaling_policies` allowlist + field-specific grammar validators; policy name/status/metric + finite bounded threshold/target; drops `FleetArn` | Typed sanitized error; empty+token → `incomplete`; malformed shape → `incomplete` w/ `malformed_response` | **Migrated.** |

Proof (unit + integration): `tests/unit/test_gamelift_projections_unit.py` and
`tests/integration/test_gamelift_projections_integration.py` assert, with
synthetic sensitive values, that account IDs, full ARNs, URLs/network
coordinates, arbitrary nested fields, provider exception text, and unbounded
collections never appear in the projection; that allowed fields carrying attack
values (short ARNs/URLs/account IDs/IPs, control characters, malformed
region/enum/name values, huge strings, wrong scalar types, nested blobs,
NaN/Inf, oversized numbers/collections) are dropped by field-specific grammar;
that the aggregate budget holds against the actual serialized JSON
(`len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS`); that the sanitized
failure log omits the raw provider message and the caller fleet id (captured
from the real Loguru sink); that empty/denied/incomplete/truncated stay distinct,
an empty page carrying a `NextToken` is `incomplete` (never a complete `empty`),
and a malformed shape (missing/wrong collection type, non-dict items, all-invalid
rows) is typed `incomplete`/`malformed_response`; and that `not_found` is pinned
via `error.code`. `tests/unit/test_gamelift_chart_routing_unit.py` proves the
projected outputs remain chart-compatible and asserts the exact runtime tool
registration collection (`GAMELIFT_AGENT_TOOLS`) passed to the specialist
factory. `tests/unit/test_provider_inventory_accuracy_unit.py` pins this
inventory's corrected claims to the code they describe.

## Knowledge Base retrieval (Bedrock KB via `kb_retrieve`) — model-visible, NOT projected

Each specialist (GameLift, EKS, Cost) registers a per-KB `kb_retrieve` tool
(`backend/src/utils/kb_tools.py`, wired in `backend/src/agents/base_specialist.py`).
Its result reaches model context directly.

| Surface | Pagination / bounds | Sensitive fields possible | Transform | Error / partial | Disposition |
| --- | --- | --- | --- | --- | --- |
| `kb_retrieve` (`strands_tools.retrieve` → Bedrock `Retrieve`) | `numberOfResults` is **caller/model-supplied** (default 3) and forwarded to the provider **unchanged** — there is no code-owned upper bound on the count; **no per-chunk size or content bound to the model**; results cached by query hash | Retrieved document chunk text and metadata (`location`, source URIs, S3 keys, score), plus whatever was ingested into the KB | **None** — the raw retrieval result is returned to the model unchanged | Underlying tool error text may reach the model | **Follow-up.** Provider-backed retrieval with no code-owned projection and no code-owned count cap; content safety depends on what is ingested into each KB, not on a projection boundary. |

## EKS specialist (MCP: `aws-api-mcp-server` via `call_aws`, `eks-mcp-server`) — NOT migrated

| Surface | Pagination / bounds | Sensitive fields possible | Transform | Error / partial | Disposition |
| --- | --- | --- | --- | --- | --- |
| `call_aws` (`aws eks list-clusters`, `describe-cluster`, resource discovery) | Provider/CLI paging; **unbounded** to model | Cluster ARNs (account ID), endpoint URLs, VPC/subnet/security-group IDs, `certificateAuthority` data, OIDC issuer URLs, arbitrary CLI JSON | **None** — raw MCP tool result reaches the model | MCP/CLI error text reaches model | **Follow-up.** No code-owned projection; raw provider JSON and exception text are model-visible. |
| `eks-mcp-server` in-cluster reads (pods, deployments, services) | Kubernetes API paging; unbounded to model | Pod IPs, node IPs, image references, env, annotations, labels | None (read-only RBAC excludes secrets) | MCP error text reaches model | **Follow-up.** RBAC bounds *what* is readable, not the projection shape. |

## Cost specialist (owned `get_cost_report` + guarded Billing MCP) — partially controlled

| Surface | Pagination / bounds | Sensitive fields possible | Transform | Error / partial | Disposition |
| --- | --- | --- | --- | --- | --- |
| `get_cost_report` (owned Cost Explorer path) | One grouped query, `Decimal`-aggregated, cached by report ID | Financial figures are the payload; no ARNs | Deterministic owned rendering; validated snapshot | Owned; `estimated` vs finalized noted | Controlled by the deterministic cost path + `financial_guard`. |
| `reuse_cost_report` (owned, cached) | **Issues no new Cost Explorer query** — reuses a cached report ID, optionally computing a combined share for named services | Financial figures from the already-validated snapshot; no ARNs | Deterministic owned rendering (`render_cost_report` / `render_cost_report_selection`) of the cached snapshot | Owned; `CostReportError` → validated error payload | Controlled deterministic surface; registered alongside `get_cost_report` (`backend/src/agents/cost_report.py`, `return [get_cost_report, reuse_cost_report]`). No-new-query semantics keep follow-up arithmetic consistent with the original snapshot. |
| Billing MCP `cost-explorer` tool | MCP paging | Raw Cost Explorer JSON, service identifiers | `cost_mcp_guard` blocks `getCostAndUsage*`; forecast/optimization pass through raw | MCP error text reaches model for allowed ops | **Follow-up.** Allowed forecast/optimization operations return raw MCP JSON with no code-owned projection. |
| Other Billing MCP operations (forecast, rightsizing, optimization) | MCP paging; unbounded to model | Resource identifiers, arbitrary MCP JSON | None | MCP error text reaches model | **Follow-up.** |

## Summary of dispositions

- **Done this wave:** the three raw GameLift Servers detail tools
  (`get_fleet_utilization`, `get_fleet_capacity`, `get_scaling_policies`) now use
  bounded, code-owned projections with field-specific grammar validation
  (identifier/region/enum/name grammars rejecting control chars, ARN prefixes,
  account-ID patterns, URL schemes, and IP/network coordinates even when short),
  an aggregate payload budget measured against the actual serialized JSON, typed
  sanitized errors, a sanitized failure log (no raw provider text or caller fleet
  id), and distinct empty/denied/incomplete/truncated status. A malformed
  response shape (missing/wrongly typed collection, non-dict items, all-invalid
  rows) is typed `incomplete`/`malformed_response`, never an authoritative
  `empty`; `not_found` is pinned via `error.code`.
- **Classic and container fleet listing (`list_gamelift_fleets`):** the container
  path is field-allowlisted but **not item- or value-capped**; the classic path
  still returns **raw `describe_fleet_attributes` items (including
  `FleetArn`/account ID)** and raw provider warning text and is **also not
  item-capped** — explicit **follow-up** (both paths uncapped to the model).
- **Knowledge Base retrieval (`kb_retrieve`, all three specialists):** returns raw
  Bedrock retrieval chunks/metadata to the model with no code-owned projection,
  and its `numberOfResults` is caller/model-supplied and forwarded unchanged with
  **no code-owned count bound** — **follow-up**.
- **Cost specialist (owned deterministic path):** both `get_cost_report` and the
  cached, no-new-query `reuse_cost_report` are registered deterministic surfaces
  rendered by the owned validated path; the guarded Billing MCP forecast/
  optimization operations still return raw provider JSON — **follow-up**.
- **Remaining (explicit follow-ups, no GitHub writes here):** EKS `call_aws`
  resource discovery, EKS in-cluster reads, and the allowed Billing MCP
  forecast/optimization operations all still return raw provider JSON and raw
  exception text to the model with no code-owned bound or projection.
