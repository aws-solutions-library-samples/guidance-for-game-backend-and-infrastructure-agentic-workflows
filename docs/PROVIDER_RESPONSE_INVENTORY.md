# Provider Response Inventory (model-visible surfaces)

Part of #457. This is a public-safe inventory of every provider response that
can currently reach model context in the default read-only chat path: SDK
(boto3) tools, MCP tools, and owned deterministic tools. It records, per
operation, the pagination/bounds, the sensitive or customer-controlled fields
the raw response can carry, the transform applied before the model sees it, the
error/partial semantics, and a disposition decision.

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
`not_found`, `throttled`, `invalid_request`, `provider_error`.

## GameLift specialist (boto3, code-owned) — MIGRATED in this wave

| Operation | Pagination / bounds | Sensitive / customer-controlled fields in raw response | Transform | Error / partial semantics | Disposition |
| --- | --- | --- | --- | --- | --- |
| `list_gamelift_fleets` (`list_fleets` + `describe_fleet_attributes` + container APIs) | Paginates all fleet IDs; chunks describe at 100 | `FleetArn` (account ID), `FleetId`, `LogGroupArn`, location detail | Code-owned `_summarize_container_fleet` / `_compact_dict` allowlist; drops `FleetId`/ARNs; `Warnings[]` list | Per-source `Warnings`; `error` only when totally empty | Already projected (pre-existing). Warning text retains provider message — **follow-up** to route through the sanitized error vocabulary. |
| `get_fleet_utilization` (`describe_fleet_utilization`) | Single call; item cap `GAMELIFT_MAX_PROJECTED_ITEMS=100`; residual `NextToken` → partial | `FleetArn` (account ID), arbitrary/future item fields, nested blobs | `project_fleet_utilization` allowlist: process/session/player counts + `Location`; drops `FleetArn` and unknown fields | Typed sanitized error; `NextToken` → `truncated`/`incomplete` | **Migrated.** ok/empty/denied/incomplete/truncated distinct. |
| `get_fleet_capacity` (`describe_fleet_capacity`) | Single call; item cap 100 | `FleetArn`, `ManagedCapacityConfiguration`, arbitrary/future fields | `project_fleet_capacity` allowlist: `InstanceType`, `Location`, bounded `InstanceCounts` / `GameServerContainerGroupCounts` | Typed sanitized error | **Migrated.** |
| `get_scaling_policies` (`describe_scaling_policies`) | Single call; item cap 100 | `FleetArn`, arbitrary/future fields, endpoints | `project_scaling_policies` allowlist: policy name/status/metric/threshold/target; drops `FleetArn` | Typed sanitized error | **Migrated.** |

Proof (unit): `tests/unit/test_gamelift_projections_unit.py` asserts, with
synthetic sensitive values, that account IDs, full ARNs, URLs/network
coordinates, arbitrary nested fields, provider exception text, and unbounded
collections never appear in the projection, and that empty/denied/incomplete/
truncated stay distinct.

## EKS specialist (MCP: `aws-api-mcp-server` via `call_aws`, `eks-mcp-server`) — NOT migrated

| Surface | Pagination / bounds | Sensitive fields possible | Transform | Error / partial | Disposition |
| --- | --- | --- | --- | --- | --- |
| `call_aws` (`aws eks list-clusters`, `describe-cluster`, resource discovery) | Provider/CLI paging; **unbounded** to model | Cluster ARNs (account ID), endpoint URLs, VPC/subnet/security-group IDs, `certificateAuthority` data, OIDC issuer URLs, arbitrary CLI JSON | **None** — raw MCP tool result reaches the model | MCP/CLI error text reaches model | **Follow-up.** No code-owned projection; raw provider JSON and exception text are model-visible. |
| `eks-mcp-server` in-cluster reads (pods, deployments, services) | Kubernetes API paging; unbounded to model | Pod IPs, node IPs, image references, env, annotations, labels | None (read-only RBAC excludes secrets) | MCP error text reaches model | **Follow-up.** RBAC bounds *what* is readable, not the projection shape. |

## Cost specialist (owned `get_cost_report` + guarded Billing MCP) — partially controlled

| Surface | Pagination / bounds | Sensitive fields possible | Transform | Error / partial | Disposition |
| --- | --- | --- | --- | --- | --- |
| `get_cost_report` (owned Cost Explorer path) | One grouped query, `Decimal`-aggregated, cached by report ID | Financial figures are the payload; no ARNs | Deterministic owned rendering; validated snapshot | Owned; `estimated` vs finalized noted | Controlled by the deterministic cost path + `financial_guard`. |
| Billing MCP `cost-explorer` tool | MCP paging | Raw Cost Explorer JSON, service identifiers | `cost_mcp_guard` blocks `getCostAndUsage*`; forecast/optimization pass through raw | MCP error text reaches model for allowed ops | **Follow-up.** Allowed forecast/optimization operations return raw MCP JSON with no code-owned projection. |
| Other Billing MCP operations (forecast, rightsizing, optimization) | MCP paging; unbounded to model | Resource identifiers, arbitrary MCP JSON | None | MCP error text reaches model | **Follow-up.** |

## Summary of dispositions

- **Done this wave:** the three raw GameLift Servers tools now use bounded,
  code-owned projections with typed sanitized errors and distinct
  empty/denied/incomplete/truncated status.
- **Pre-existing:** `list_gamelift_fleets` already projects; its warning text
  should be moved onto the shared sanitized error vocabulary (follow-up).
- **Remaining (explicit follow-ups, no GitHub writes here):** EKS `call_aws`
  resource discovery, EKS in-cluster reads, and the allowed Billing MCP
  forecast/optimization operations all still return raw provider JSON and raw
  exception text to the model with no code-owned bound or projection.
