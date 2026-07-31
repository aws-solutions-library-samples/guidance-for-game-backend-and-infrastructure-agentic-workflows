# Architecture Decision Records

These records define accepted architecture boundaries for the planned optional
operations control plane.

An accepted record is a constraint on future implementation. It does not mean
that the described infrastructure, permissions, API, or user interface is
currently deployed. The existing Game Agent chat path remains the current
product behavior.

| Record | Decision |
|---|---|
| [ADR 0001](0001-preserve-chat-and-add-optional-operations.md) | Preserve chat and add an optional operations control plane |
| [ADR 0002](0002-use-protocol-neutral-operations-services.md) | Use protocol-neutral operations services and shared adapters |
| [ADR 0003](0003-isolate-provider-writes.md) | Isolate provider writes behind prepared executors |

Later architecture issues define versioned contracts, trusted principal
context, persistence, workflow recovery, and incremental cost. Those details
must not weaken the boundaries accepted here.
