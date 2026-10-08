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
| `truncated` | result set bounded by the projection's item cap or the serialized-payload budget (a distinct `status`; a partial view is additionally flagged with the boolean marker `truncated: true`) |

A terminal malformed collection with no usable rows uses `status` ==
`incomplete` with `error.code` == `malformed_response` and no truncation marker.
A mixed collection that retains valid rows while discarding malformed rows uses
the same status and error code plus the boolean marker `truncated: true` to show
that its retained view is partial. The `truncated` **status** remains reserved
for a wholly valid payload bounded only by the item or serialized-size cap.

Typed, sanitized error codes (no provider text): `access_denied`,
`not_found`, `throttled`, `invalid_request`, `provider_error`,
`malformed_response` (the response shape itself was unusable — collection
missing/wrongly typed, or every item non-projectable).

## GameLift specialist (boto3, code-owned) — MIGRATED in this wave

| Operation | Pagination / bounds | Sensitive / customer-controlled fields in raw response | Transform | Error / partial semantics | Disposition |
| --- | --- | --- | --- | --- | --- |
| `list_gamelift_fleets` (`list_fleets` + `describe_fleet_attributes` + container APIs) | Paginates all fleet IDs; chunks describe at 100; **neither classic nor container fleet output is item-bounded to the model** — both paths append every page/summary with no aggregate cap | `FleetArn` (account ID), `FleetId`, `LogPaths`, `MetricGroups`, `ScriptId`/`BuildId`, `InstanceRoleArn`, location detail, arbitrary future attributes | **Container fleets:** code-owned `_summarize_container_fleet` allowlist. **Classic fleets: NONE** — raw `describe_fleet_attributes` items are appended unchanged and exposed under `FleetAttributes` (and `ClassicFleets`); `Warnings[]` carry raw provider `str(e)` text | Per-source `Warnings` (raw provider text); `error` only when totally empty | **Partially projected.** Container path is field-allowlisted but **not item- or value-capped**; the classic path returns **raw `FleetAttributes` including `FleetArn`/account ID and raw provider warning text** and is **also not item-capped**, and neither path is routed through the sanitized error vocabulary — **follow-up** to project the classic path, cap both paths, and sanitize warnings. |
| `get_fleet_utilization` (`describe_fleet_utilization`) | Single call; item cap `GAMELIFT_MAX_PROJECTED_ITEMS=100` + aggregate `GAMELIFT_MAX_PROJECTED_CHARS` budget measured against the **actual final serialized envelope** (the exact `status`/`truncated`/`error` keys plus escaping the model will see), so a mixed/malformed disposition cannot outgrow the cap; residual `NextToken` (incl. empty page) → partial | `FleetArn` (account ID), arbitrary/future item fields, nested blobs | `project_fleet_utilization` allowlist + **field-specific grammar validators** (GameLift identifier/region/enum/name grammars; rejects control chars, ARN prefixes, account-ID patterns, URL schemes, IP/network coordinates — including a valid IPv4 quad embedded inside a longer dotted run — and malformed values even when short; finite bounded numbers); drops `FleetArn`, unknown fields, and allowed fields carrying an invalid value | Typed sanitized error; `NextToken` → `incomplete`; wholly-valid item/char-cap bound → `truncated` **status**; empty+token → `incomplete`; terminal malformed shape → `status` `incomplete` with `malformed_response`; a mixed collection retaining valid rows also sets `truncated: true` | **Migrated.** ok/empty/denied/incomplete/truncated distinct; not_found and malformed pinned via `error.code`; only mixed retained views add the `truncated: true` marker to `incomplete`/`malformed_response`. |
| `get_fleet_capacity` (`describe_fleet_capacity`) | Single call; item cap 100 + serialized-envelope char budget (measured against the final envelope) | `FleetArn`, `ManagedCapacityConfiguration`, arbitrary/future fields | `project_fleet_capacity` allowlist + field-specific grammar validators; `InstanceType`, `Location`, and bounded/validated `InstanceCounts` / `GameServerContainerGroupCounts` numbers | Typed sanitized error; empty+token → `incomplete`; terminal malformed shape → `incomplete` with `malformed_response`; mixed retained rows also set `truncated: true` | **Migrated.** |
| `get_scaling_policies` (`describe_scaling_policies`) | Single call; item cap 100 + serialized-envelope char budget (measured against the final envelope) | `FleetArn`, arbitrary/future fields, endpoints | `project_scaling_policies` allowlist + field-specific grammar validators; policy name/status/metric + finite bounded threshold/target; drops `FleetArn` | Typed sanitized error; empty+token → `incomplete`; terminal malformed shape → `incomplete` with `malformed_response`; mixed retained rows also set `truncated: true` | **Migrated.** |

Proof (unit + integration): `tests/unit/test_gamelift_projections_unit.py` and
`tests/integration/test_gamelift_projections_integration.py` assert, with
synthetic sensitive values, that account IDs, full ARNs, URLs/network
coordinates, arbitrary nested fields, provider exception text, and unbounded
collections never appear in the projection; that allowed fields carrying attack
values (short ARNs/URLs/account IDs/IPs, control characters, malformed
region/enum/name values, huge strings, wrong scalar types, nested blobs,
NaN/Inf, oversized numbers/collections) are dropped by field-specific grammar;
that the aggregate budget holds against the actual **final serialized envelope**
(`len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS`, including the mixed
malformed disposition, which cannot outgrow the cap); that a valid IPv4 quad
embedded inside a longer dotted run (`host-10.11.12.13.14-prod`) is rejected;
that the sanitized
failure log omits the raw provider message and the caller fleet id (captured
from the real Loguru sink); that empty/denied/incomplete/truncated stay distinct,
an empty page carrying a `NextToken` is `incomplete` (never a complete `empty`),
and a terminal malformed shape (missing/wrong collection type or all-invalid rows) is typed
`incomplete`/`malformed_response` without a truncation marker, while a mixed
collection that retains valid rows adds the boolean `truncated: true` marker
(neither case uses the `truncated` status); and `not_found` is pinned
via `error.code`. `tests/unit/test_gamelift_chart_routing_unit.py` proves the
projected outputs remain chart-compatible and asserts the exact runtime tool
registration collection by injecting a fake `agent_factory` into
`build_gamelift_agent` and observing the factory's actual `call_args`
(`additional_tools` == `GAMELIFT_AGENT_TOOLS`); production builds default to the
real `create_specialist_agent`. `tests/unit/test_provider_inventory_accuracy_unit.py` pins this
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
| Billing MCP `cost-explorer` tool | Forecast MCP paging | Raw Cost Explorer forecast JSON, service identifiers | `cost_mcp_guard` blocks `getCostAndUsage*` (case-insensitive redirect to `get_cost_report`); the only forecast operation is `getCostForecast`, projected by `agents.cost_projections.project_forecast` into a bounded, `estimated` envelope (total amount+unit, bounded per-period mean/prediction-interval values, time periods). The forecast is scoped by code-owned flat `services` (1-10 SERVICE display names) and `regions` (1-10 region codes) that the guard turns into a canonical `Dimensions` expression (an `And` of both when present); the model never supplies raw filter JSON, and the applied scope (or `account`) is echoed in the envelope | Typed sanitized error (`access_denied`/`not_enrolled`/`not_found`/`throttled`/`invalid_request`/`data_unavailable`/`credentials_unavailable`/`provider_error`/`malformed_response`), each with its own code-owned guidance message; no raw provider text; distinct `empty`/`incomplete`/`truncated`; a missing/wrong-typed results list (absent counts as malformed, not empty) or an invalid `Total` is `incomplete`+`partial`+`malformed_response` | **Migrated.** Only `getCostForecast` is model-visible (per-granularity horizon: ~3 months DAILY, 18 months MONTHLY); `getUsageForecast` and historical ops are not model-visible. |
| Billing MCP `compute-optimizer` tool | MCP paging; code-owned item cap + final serialized-byte budget; the guard over-fetches by one row to detect residual data | `instanceArn`/account ID, tags, free-text names (including the Auto Scaling group's customer-chosen name), arbitrary nested config | `project_compute_optimizer` allowlist + field grammars: model-safe resource id derived from the ARN suffix (for an Auto Scaling group this suffix is the customer-chosen group name, admitted only through the bounded identifier grammar) or omitted; finding/current/top-N recommended types; bounded estimated savings + currency; account IDs, ARNs, tags, continuation tokens dropped | Typed sanitized error; residual `next_token` OR more rows than requested → `incomplete`+`paginated`+`partial`; a single over-fetch probe row at the item cap stays `paginated`+`partial` (not `truncated`); only more than `MAX_ITEMS + 1` valid rows (which only a provider that returns more than the over-fetch requested can produce) → `truncated`; a mixed collection that retains valid rows while dropping malformed rows → `incomplete`/`malformed_response`+`partial` (the `truncated` marker is set only when valid rows exceed `MAX_ITEMS + 1`) | **Migrated.** Cross-account params (`account_ids`) rejected before dispatch; the input `region` must be absent or equal to the deployment region (otherwise `denied_input`); only the two performance rightsizing operations (`get_ec2_instance_recommendations`, `get_auto_scaling_group_recommendations`) are allowed. EBS and Lambda rightsizing are not model-visible (their dependent read permissions were never granted). |
| Billing MCP `cost-optimization` tool (Cost Optimization Hub) | MCP paging; the upstream list helpers paginate internally and truncate to exactly `maxResults` with NO continuation token, so the guard requests `bound + 1` and the projector flags residual data (`paginated` + `partial`) when the extra row arrives; code-owned item cap + final serialized-byte budget | `recommendationId`/`resourceArn`/account ID, tags, nested `recommendedResourceDetails` blobs | `project_cost_optimization_*` allowlist: bounded recommendation id/region/resource id (grammar-validated), action/effort tokens, restart/rollback booleans, bounded estimated savings+percentage+currency, source, refresh time; account IDs, ARNs, tags, arbitrary nested blobs dropped | Typed sanitized error; `empty`/`incomplete`/`truncated`/`paginated` distinct; a list truncated by the helper's own cap is `incomplete`+`paginated`+`partial` (never a silent `ok`); `group_by=AccountId` rejected; `ResourceNotFoundException` → `not_found` | **Migrated.** Only `list_recommendation_summaries` (non-AccountId `group_by`), `list_recommendations`, `get_recommendation` are allowed; `get_recommendation` takes the `recommendation_id` from `list_recommendations` in `resource_id`. |
| Other Billing MCP operations (budgets, anomaly, free tier, invoicing, cost comparison, RI/SP performance, rec-details, storage-lens, session-sql, pricing, billing conductor/views, cost categories/allocation tags, pricing calculator, compute-optimizer automation) | n/a | Actual historical spend, resource identifiers, arbitrary MCP JSON | **Dropped from the model-visible catalog by `cost_mcp_guard`** (tool allowlist). Several return actual historical spend and would bypass the deterministic report path | Not reachable | **Removed.** No longer model-visible; the corresponding IAM grant narrowing is tracked separately in #482. |

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
  rendered by the owned validated path; the guarded Billing MCP forecast and
  optimization operations (`cost-explorer` cost-forecast only with code-owned
  service/region scope, `compute-optimizer`, `cost-optimization`) are now
  **migrated** — each has a code-owned bounded projection (`agents.cost_projections`)
  with typed sanitized errors, an `estimated` flag, distinct
  empty/incomplete/truncated/paginated states (a helper-truncated Hub list is
  flagged `incomplete`+`paginated`, never a silent `ok`), and a final
  serialized-byte budget, and every other Billing MCP tool is dropped from the
  model-visible catalog.
- **Remaining (explicit follow-ups, no GitHub writes here):** EKS `call_aws`
  resource discovery and EKS in-cluster reads still return raw provider JSON and
  raw exception text to the model with no code-owned bound or projection. The
  allowed Billing MCP forecast and optimization operations are migrated (see the
  Cost specialist bullet above) and are no longer a follow-up.
