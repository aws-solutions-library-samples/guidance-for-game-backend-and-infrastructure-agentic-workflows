# Architecture Decision Records

These records define accepted and proposed architecture boundaries for the
current chat path, the planned optional operations control plane, and optional
remote access.

An accepted record is a constraint on future implementation. A proposed record
requires architecture review. Neither status means that the described
infrastructure, permissions, API, or user interface is currently deployed. The
existing Game Agent chat path remains the current product behavior.

| Record | Status | Decision |
|---|---|---|
| [ADR 0001](0001-preserve-chat-and-add-optional-operations.md) | Accepted | Preserve chat and add an optional operations control plane |
| [ADR 0002](0002-use-protocol-neutral-operations-services.md) | Accepted | Use protocol-neutral operations services and shared adapters |
| [ADR 0003](0003-isolate-provider-writes.md) | Accepted | Isolate provider writes behind prepared executors |
| [ADR 0004](0004-expose-governed-public-mcp-facade.md) | Proposed | Expose a governed public MCP facade |
| [ADR 0005](0005-persist-operations-and-recover-workflows.md) | Proposed | Persist operations state and recover workflows |

Later architecture issues define versioned contracts, trusted principal
context, and incremental cost. Those details must not weaken the boundaries
accepted here.

The published versioned records, hash rules, lifecycle, and source-control
profile are documented in [Operations Contracts](../OPERATIONS_CONTRACTS.md).
