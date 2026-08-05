# ADR 0002: Use Protocol-Neutral Operations Services

- **Status:** Accepted
- **Date:** 2026-07-30
- **Decision issue:** [#276](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/276)

## Context

Operations will eventually be requested through web, HTTP, chat, event, and
remote MCP entry points. Duplicating matching or authorization logic in those
adapters would allow behavior to drift and create bypass paths.

## Decision

Implement product rules in protocol-neutral Python application services.
Transport adapters translate requests and responses but do not own product
authorization policy.

```text
Operations UI -> Next.js adapter -> Operations HTTP API
Remote MCP client -------------> Operations HTTP API
Chat adapter -------------------> Application services
Event adapter ------------------> Application services

Operations HTTP API -> Application services
Application services -> matching, authorization, operation, ledger, capability
Application services -> provider-read-only integration or prepared executor
```

The Operations HTTP API is the stable remote service adapter. Other adapters
must use the same application contracts and authorization decision service.
Adapters can be introduced in different phases without creating a second
implementation of product rules.

### Adapter Responsibilities

An adapter:

- verifies transport authentication at a trusted boundary;
- constructs trusted request and principal context;
- rejects principal identity supplied by an operation request body;
- maps protocol data to versioned application contracts;
- supplies request and correlation identifiers; and
- maps typed application results to protocol responses.

### Application Service Responsibilities

Application services:

- perform exact playbook and capability matching;
- calculate deterministic authorization decisions;
- manage operation state and ledger events;
- enforce idempotency and workspace binding; and
- invoke only the provider integration allowed by the prepared operation.

The application layer must not import CopilotKit, Next.js, MCP, or agent-message
request types. Language-model output is untrusted proposal data until validated
by these services.

## Consequences

- The same principal and operation must produce the same authorization result
  through every adapter.
- Contract and adapter-parity tests become release gates.
- Protocol-specific authentication and error mapping stay at the edge.
- Versioned contracts and exact principal fields are defined by later E0
  issues without changing this boundary.

## Rejected Alternatives

- Implementing authorization separately in each route or tool.
- Trusting UI state, model output, or request-body identity.
- Exposing every HTTP route automatically as an MCP tool.
