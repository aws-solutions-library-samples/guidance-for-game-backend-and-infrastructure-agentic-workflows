# ADR 0003: Isolate Provider Writes

- **Status:** Accepted
- **Date:** 2026-07-30
- **Decision issue:** [#276](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/276)

## Context

The current AgentCore runtime processes untrusted natural-language input and
uses read-only provider integrations. Granting that runtime provider write
permissions would allow prompt injection, tool misuse, or adapter defects to
cross directly into infrastructure mutation.

## Decision

Keep the existing AgentCore chat role provider-read-only. Future provider
writes execute only through a separate prepared executor with a narrow IAM
role for one capability or a very small capability family.

The web UI, Operations HTTP API, chat tools, remote MCP tools, and event
adapters never perform provider mutation directly. They may request an
operation through shared application services.

A prepared executor:

- receives an operation identifier, not arbitrary code or provider commands;
- loads the validated operation record from trusted storage;
- verifies the exact prepared-operation content before execution;
- enforces hard parameter bounds, preconditions, and idempotency;
- calls only the provider action allowlisted for that capability; and
- records verification and rollback results.

Executors must not expose:

- shell access;
- arbitrary Python or generated-code execution;
- generic AWS API calls; or
- generic Kubernetes commands.

This record grants no permissions. The first write capability requires its own
implementation and security review before an executor role is deployed.

## Consequences

- Compromise of the chat runtime cannot directly invoke a provider write API.
- Each write capability has an independently reviewable permission boundary.
- Approval and prepared-operation contracts can be evaluated before execution.
- Write workflows require additional infrastructure, but that infrastructure
  is introduced only when a bounded write capability needs it.

## Rejected Alternatives

- Adding write permissions to the existing chat runtime.
- Letting adapters call provider mutations directly.
- Building a generic execution service for shell, scripts, or arbitrary APIs.
