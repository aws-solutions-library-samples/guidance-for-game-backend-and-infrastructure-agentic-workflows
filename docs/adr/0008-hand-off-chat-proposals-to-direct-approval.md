# ADR 0008: Hand Off Chat Proposals to Direct Approval

- **Status:** Proposed
- **Date:** 2026-10-08
- **Decision issue:** [#555](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/555)

## Context

[ADR 0001](0001-preserve-chat-and-add-optional-operations.md) keeps the chat
path read-only and adds the operations control plane as an optional,
default-disabled deployment. [ADR 0002](0002-use-protocol-neutral-operations-services.md)
places all product authorization in protocol-neutral application services that
adapters call but never duplicate. [ADR 0003](0003-isolate-provider-writes.md)
keeps the AgentCore chat role provider-read-only and isolates every write
behind a separate prepared executor. The E2 advice-and-approval phase (#414)
adds, as a **separate API Gateway HTTP API plus Lambda stack** with its own
Cognito JWT authorizer, the routes `POST /operations/prepare`,
`POST /operations/{operationId}/approve`, `POST /operations/{operationId}/reject`,
`POST /operations/{operationId}/cancel`, and `GET /operations/{operationId}`.
The approval boundary is defined in [Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity)
and the versioned proposal contract in [Operations Contracts](../OPERATIONS_CONTRACTS.md#trusted-context).

[#274](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/274)
states that models may propose operations and that web, chat, and other
adapters use the same application services. No record yet says how the
read-only chat assistant hands a proposal to E2 preparation, or how the user
then reaches a direct approval. Today the assistant can describe a capacity
change but cannot start one, because preparation and approval live on the
separate operations API that the chat runtime cannot and must not call with any
write or approval capability.

This record decides the handoff only. It enables no operations, grants no new
permission, and leaves the default deployment read-only and unchanged.

## Decision

### 1. The trusted frontend server proxy submits the proposal

A new server-side proxy in the frontend (the same trusted tier that holds the
#554 approve and reject proxies and the operator-console proxies) is the only
component that submits a proposal to `POST /operations/prepare`. It does so on
behalf of the verified user, carrying the user's verified Cognito access token
in the `Authorization` header exactly as the current chat path forwards it to
the AgentCore runtime (see [Repository Guidance](../../AGENTS.md) and
[Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#current-web-path)).

The chat runtime does not call the operations API. The AgentCore chat role gains
no operations permission, no provider-write permission, and no credential for
the operations stack. The handoff crosses from the browser to the frontend
server, and from the frontend server to the operations API; it never flows
through the AgentCore runtime, a chat tool, or a model tool call.

The proxy follows the same hardening the #554 proxies require: it verifies the
bound ID and access tokens, requires the server-owned group that may propose,
enforces same-origin writes, rejects redirects, and bounds response bytes.
Browser JavaScript never receives a Cognito token.

### 2. Model output becomes a typed, untrusted proposal that preparation validates

Model output is proposal data, never executable content and never authority.
When the assistant has enough observed state to describe a specific capacity
change, trusted UI code — not the model — renders a proposal affordance from the
displayed values. The user confirms the typed fields (fleet, location, requested
capacity) and the bound `observation_id` of the E1 observation the advice was
derived from. Confirming calls the proxy in decision 1.

The proxy maps those confirmed fields to the versioned
`prepare-operation-request` body, whose schema carries **only**
`observation_id` plus the capacity proposal fields and sets
`additionalProperties: false`. Any identity, tenant, workspace, policy, risk,
executor, hash, or credential field in the body fails validation at the
operations API ([Operations Contracts](../OPERATIONS_CONTRACTS.md#trusted-context)).
The `PrepareOrchestrator` recomputes advice from the trusted current-state read,
selects the playbook in code, binds identity from the verified principal, and
stores one immutable prepared operation with its canonical hash. Free text never
becomes an executable field, and the model never selects a capability, lowers
risk, or grants authority.

### 3. The user reaches approval through the direct operator approval view

Approval is a direct, authenticated UI or API action outside the chat and model
tool path. After a successful preparation the proxy returns the `operation_id`
and the stored preview to trusted UI code, which deep-links the user to the
operator approval view delivered by [#554](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/554).
That view loads the preview from the stored, immutable operation record by ID
through `GET /operations/{operationId}` and renders the action, exact
parameters, risk, requester, and expiry, then offers approve and reject actions
that call the #554 server-side proxies for the E2 approve and reject routes.

An optional in-chat authorization affordance, in the spirit of Cloudscape's
["user-authorized actions"](https://cloudscape.design/gen-ai/patterns/user-authorized-actions/)
pattern, may be rendered by trusted UI code from the stored preview the proxy
returned. It is a presentational entry point only: selecting it navigates the
user to the #554 approval view (or renders the same trusted preview-and-confirm
component the #554 view uses), and the resulting approve action calls the E2
approve route through the #554 proxy. The approve or reject decision never
travels inside a chat message, a model response, a model tool call, or any
request body beyond the empty-bodied approve/reject route. The in-chat affordance
is additive and gated behind the same default-off operations UI gate as the
operator console; the #554 approval view is the authoritative surface.

### 4. Identity, tenant, and workspace come only from verified server-side context

The proposal proxy and the operations API derive requester identity from the
verified Cognito access token, and tenant and workspace from the server-owned
`GBAW_OPERATIONS_TENANT_ID` and `GBAW_OPERATIONS_WORKSPACE_ID` bindings. The
operations API reads identity solely from its API Gateway JWT authorizer
context, never from the body, query string, or a custom header. Request-body
fields, CopilotKit data, chat text, model output, tool arguments, and
browser-supplied identity cannot establish or override requester, approver,
tenant, workspace, group, or any authorization value. The approver is a separate
verified principal supplied to the approve route, and separation of duties
follows the server-owned policy in
[Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity).

### 5. When operations are disabled or absent, the assistant stays read-only

The frontend discovers operations availability from backend capability
discovery, as [ADR 0001](0001-preserve-chat-and-add-optional-operations.md)
requires. When the operations stack is unprovisioned, when
`GBAW_OPERATIONS_MODE` is below the authority the capability needs, or when
capability discovery does not confirm the proposal capability, trusted UI code
does not render the proposal or in-chat authorization affordance. The assistant
states plainly that it can describe but not start a change, and the chat path
remains read-only. A hidden or stale UI control is never authorization: the
operations API and its JWT authorizer fail closed independently if a proposal or
approval request arrives while operations are disabled, and a provisioned but
disabled stack throttles its API stage to zero.

### 6. Submission limits

Proposal submission is rate-bounded at the proxy per verified subject, in
addition to the WAF rate limiting on the frontend edge. The operations API
enforces workspace-scoped idempotency: a byte-identical retry under the same
idempotency token replays the stored operation, and a changed intent under the
same token fails with `IDEMPOTENCY_CONFLICT`
([Operations Contracts](../OPERATIONS_CONTRACTS.md#workflow-identity-and-idempotency)).
The proxy supplies a fresh idempotency token per distinct user confirmation and
reuses it on a transport retry, so a double-submit from the browser cannot
create two operations. Preparation does not touch a provider, so a submission
produces at most one immutable `pending_approval` record awaiting a direct human
approval, never a provider write.

### 7. Audit

Every proposal submission, preparation outcome, and approval or rejection is an
append-only ledger event on the operation, keyed by `operation_id`, recorded by
the operations application services and bounded and redacted in the
`GET /operations/{operationId}` evidence view. The ledger records the requester
and approver source so that an approval can never be attributed to chat text or
model output. The proxy and operations handlers log a request ID and redacted
identifiers only; raw tokens, cookies, email addresses, and display names are
never logged, consistent with
[Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#logging). The
prepared operation stores opaque subject and client identifiers, never an email
address or display name.

## Preserved Invariants

- The chat runtime never gains a provider-write permission or an operations
  credential. The AgentCore chat role stays provider-read-only
  ([ADR 0003](0003-isolate-provider-writes.md)).
- Approval never travels through a chat message, a model response, a model tool
  call, or any request body. It is a direct authenticated UI or API action on
  the E2 approve route ([Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity)).
- Model output is untrusted proposal data. It never selects a capability,
  grants authority, or lowers risk; preparation validates and binds everything
  ([ADR 0002](0002-use-protocol-neutral-operations-services.md)).
- The default deployment remains read-only and provisions no operations
  resource ([ADR 0001](0001-preserve-chat-and-add-optional-operations.md)).

## Contract Impact

The E2 proposal contract (`prepare-operation-request`, contract version `1.0`)
meaning is **not** mutated by this record. The handoff uses the existing body
shape: `observation_id` plus the capacity proposal fields, with
`additionalProperties: false`. No field is added, removed, or reinterpreted.

One additive, versioned change is listed for a future phase, not adopted here:
if a capability-discovery response needs to advertise which proposal
capabilities the frontend may render an affordance for, that is a new
capability-discovery contract with its own version field and schema, published
under the rules in [Operations Contracts](../OPERATIONS_CONTRACTS.md#compatibility-and-publication).
It does not change the proposal, prepared-operation, approval, or any existing
v1 contract. Until it exists, the frontend relies on the existing
capability-discovery signal and the default-off gate.

## Consequences

- The handoff reuses the trusted frontend proxy tier and the #554 approval view,
  so no new service and no new trust boundary type is introduced; a single new
  trust boundary (chat proposal submission) is added to the threat model.
- The chat assistant can turn observed state into a reviewable proposal without
  ever holding write or approval authority, and the user approves on a surface
  the model cannot reach.
- The in-chat authorization affordance is optional and purely presentational,
  so the authoritative approval surface stays the #554 operator view and a
  future UI shell change ([#546](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/546))
  does not change the trust boundary.
- A disabled or unprovisioned operations stack leaves the assistant read-only
  with a clear message, and the API fails closed regardless of any UI state.
- The frontend gains a proposal proxy that must be reviewed to the same bar as
  the #554 approve and reject proxies.

## Rejected Alternatives

- **Let the chat runtime call the operations prepare API.** Rejected: it would
  require giving the provider-read-only AgentCore role an operations credential
  and a network path to the write-adjacent control plane, expanding the chat
  runtime's blast radius and violating [ADR 0003](0003-isolate-provider-writes.md).
  Model output or a prompt injection could then drive preparation directly.
- **Build a new standalone proposal-submission service.** Rejected: it would
  duplicate the trusted-proxy authentication, group check, and token-forwarding
  that the #554 proxies already implement, creating a second code path to keep
  in parity ([ADR 0002](0002-use-protocol-neutral-operations-services.md)) for
  no capability the proxy tier lacks.
- **Render the approve action inside the chat transcript and call the approve
  route from a model tool.** Rejected: approval would then travel through model
  output and a model tool call, which the approval boundary forbids. The model
  must never be able to synthesize or invoke an approval
  ([Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity)).
- **Have the model emit the typed proposal body directly as structured output
  and submit it unchanged.** Rejected: the body would be untrusted model output.
  Trusted UI code renders the affordance from displayed state and the user
  confirms the fields, and preparation re-derives and validates everything
  regardless, so model-authored structure is never trusted as the proposal.
- **Add identity, tenant, or workspace fields to the proposal body so chat can
  pass context.** Rejected: it would mutate the v1 proposal contract and create
  a body-supplied-identity bypass. These values are server-owned
  ([Operations Contracts](../OPERATIONS_CONTRACTS.md#trusted-context)).
