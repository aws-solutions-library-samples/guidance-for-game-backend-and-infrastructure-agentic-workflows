# ADR 0004: Expose a Governed Public MCP Facade

- **Status:** Proposed
- **Date:** 2026-08-06
- **Decision issue:** [#269](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/269)

## Context

Game Agent currently serves the web user interface through an HTTP AgentCore
Runtime. The orchestrator selects specialist agents. Some specialists use
embedded Model Context Protocol (MCP) servers through private standard input
and standard output connections.

Users also need access from remote MCP clients, such as desktop applications or
their own agent systems. This access must not replace or change the current web
path.

A large language model is probabilistic. It can give different results for
similar inputs. It can also select an incorrect tool or follow instructions
from untrusted content. A model is useful for interpretation, routing,
recommendations, and drafting. It cannot grant authority or replace
deterministic validation.

Publishing each specialist or embedded MCP tool would expose internal
composition as a public contract. It would also increase the tool and
permission surface and let an external model bypass the orchestrator.

## Decision

Keep the current web and HTTP AgentCore Runtime path as the default experience.
Add a separate, optional public MCP deployment that is absent by default.

Use AgentCore Gateway as the public MCP entry point. Route it to a separate
AgentCore Runtime that uses the MCP server protocol. Restrict direct access to
that runtime so clients cannot bypass Gateway authentication, policy, rate
limits, or audit.

Expose one small public facade with stable, versioned tool contracts. The first
tool set is:

- `game_agent.ask`
- `game_agent.list_capabilities`

The `game_agent.ask` tool calls the same protocol-neutral conversation service
as the HTTP adapter. That service calls the orchestrator. The orchestrator,
specialist agents, and embedded MCP tools remain private implementation
details.

Both adapters must construct the same immutable principal context from verified
authentication data. For direct end-user clients, use OAuth 2.0 and verified
JSON Web Tokens. Validate the issuer, signature, expiration, audience, client
identifier, scopes, and required tenant, workspace, group, or role claims.

An optional AWS Signature Version 4 path may support service-to-service clients.
Because one Runtime has one inbound authentication type, the end-user and
service paths require separate Runtime versions or deployments. Caller
authentication does not grant the caller the Runtime role's downstream AWS
permissions.

Tool arguments, request bodies, model output, and custom user headers cannot
provide or replace principal fields. Bind server-controlled conversation and
memory identifiers to the verified tenant, workspace, and principal.

The first public MCP release is read-only. Later releases may add versioned
tools that prepare an operation, get its status, or request cancellation. Those
tools must call the shared operations application services defined by
[ADR 0002](0002-use-protocol-neutral-operations-services.md). They cannot
approve an operation or perform a provider write.

Preparing an operation does not approve it or grant execution authority. The
public MCP facade and Gateway cannot invoke a prepared executor directly. An
operation identifier is not a credential or authorization.

All provider writes remain behind the prepared executors defined by
[ADR 0003](0003-isolate-provider-writes.md). The MCP Runtime and Gateway receive
no provider write credential or permission.

Every public tool call must use deterministic schema validation,
authorization, workspace binding, rate limits, and audit. The same principal
and request must produce the same authorization result through HTTP and MCP.

## Consequences

- Existing deployments and web clients remain unchanged unless an owner
  deploys and enables the MCP access path.
- The default deployment creates no AgentCore Gateway or MCP Runtime resources
  and adds no MCP cost.
- Internal agents and MCP tools can change without changing the public tool
  contract.
- HTTP and MCP adapters require contract-parity and authorization-parity tests.
- The optional MCP path adds a separate Runtime, Gateway configuration,
  authentication setup, monitoring, and cost.
- Conversation continuity across clients requires an explicit,
  principal-bound session mapping.
- A smaller public tool set limits client flexibility but reduces confused
  deputy, tool injection, and contract-drift risks.

## Rejected Alternatives

- Replacing the current HTTP AgentCore Runtime and web path with MCP.
- Publishing the orchestrator and each specialist as separate public tools.
- Relaying embedded MCP tools through the public MCP server.
- Generating a public MCP tool for every HTTP route.
- Trusting identity or workspace fields supplied in tool arguments.
- Treating a desktop user's AWS identity as automatic downstream AWS
  authorization.
- Treating an operation identifier as approval or executor authorization.
- Exposing a prepared executor as a public MCP tool or Gateway target.
- Letting an MCP tool approve or execute a provider write.
- Allowing clients to invoke the MCP Runtime directly and bypass Gateway
  controls.
