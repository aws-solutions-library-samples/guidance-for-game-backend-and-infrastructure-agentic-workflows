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
Cognito JWT authorizer, the routes `POST /operations/observe`,
`POST /operations/prepare`, `POST /operations/{operationId}/approve`,
`POST /operations/{operationId}/reject`, `POST /operations/{operationId}/cancel`,
and `GET /operations/{operationId}`. The approval boundary is defined in
[Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity)
and the versioned proposal contracts in
[Operations Contracts](../OPERATIONS_CONTRACTS.md#trusted-context).

[#274](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/274)
states that models may propose operations and that web, chat, and other
adapters use the same application services. No record yet says how the
read-only chat assistant hands a proposal to E2 preparation, or how the user
then reaches a direct approval. Today the assistant can describe a capacity
change but cannot start one, because preparation and approval live on the
separate operations API that the chat runtime must not call with any write or
approval capability. [#429](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/429)
requires that preparation remains unreachable from the chatbot.

This record decides the handoff only. It enables no operations, grants no new
runtime permission, and leaves the default deployment read-only and unchanged.
Every surface it names (the proposal proxy, the prepare API, the approval view,
and token-audience separation) is a planned implementation gated behind the
optional operations stack and the child issues in the Implementation section.

### A residual shared-token exposure this record must close

In the current hosted design a single Cognito **access token** is forwarded by
the chat path to the AgentCore runtime
([`ui/src/pages/api/copilot/chat.ts`](../../ui/src/pages/api/copilot/chat.ts),
Authorization bearer; the runtime reads the raw header from its request
context). The E2 operations HTTP API authorizer is configured to accept the
**same** Cognito app-client audience. The AgentCore chat role holds no IAM
permission for the operations stack, but a forwarded bearer token is itself a
usable credential: runtime code that can make an authenticated HTTP call could
replay that token against `POST /operations/prepare` and `/cancel`, and for an
`admin` user against `/approve` and `/reject` on another requester's pending
operation. IAM separation does not close this; token-audience separation does.

Decision 1 therefore makes token-audience separation a **blocker** of E2
proposal activation (#441), and this record stops asserting that the chat
runtime holds "no credential" until that separation is implemented. The same
residual already applies to the E1 read-only observation API today, because it
shares the same authorizer audience, and the same mechanism closes both.

## Decision

### 1. The operations API must accept only a token the chat runtime never receives

The operations HTTP API (observe, prepare, approve, reject, cancel, and the
evidence read) MUST authorize only a bearer token whose audience or scope the
chat-forwarded token cannot carry. This is a **blocker of E2 proposal
activation (#441)** and also closes the equivalent residual on the E1
observation API deployed under the same authorizer today.

**Chosen mechanism — a separate operations Cognito app client (audience),
obtained server-side by the frontend through the OAuth 2.0 authorization-code
grant with PKCE.**

- A second Cognito app client is provisioned for the operations API. The E2/E1
  API Gateway JWT authorizer lists **only** that operations client in its
  `Audience`; the AgentCore authorizer keeps **only** the existing chat client.
  A token minted for the chat client is rejected (401) by every operations
  route, and a token minted for the operations client is never forwarded to the
  AgentCore runtime.
- The frontend already signs the user in to the chat client with SRP via
  `amazon-cognito-identity-js`. To obtain the operations-audience token without
  exposing it to the browser, the trusted frontend server performs a server-side
  OAuth 2.0 authorization-code exchange with PKCE against the Cognito
  [managed login / Hosted UI](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-app-integration.html)
  `/oauth2/authorize` and `/oauth2/token` endpoints for the operations client,
  using the user's existing authenticated Cognito session, and keeps the
  resulting access token server-side in an `HttpOnly`, `Secure`, `SameSite=Lax`
  cookie scoped to the operations proxies. Browser JavaScript never receives
  either token. The authorization-code grant with PKCE is Cognito's documented
  flow for a confidential/public web client obtaining tokens via the managed
  login endpoints; it works on the user pool's standard feature plan and adds no
  per-feature plan cost beyond user-pool MAU pricing.
- **Trade-offs.** This adds a managed-login/Hosted-UI domain and a second app
  client to the base stack, and a short server-side code-exchange step on first
  operations use. It introduces no token into the browser and no new per-request
  cost. It is reversible: removing the operations client and its proxies returns
  the deployment to chat-only.

**Alternatives (recorded, not chosen):**

- **A required custom OAuth scope on the operations routes
  (`AuthorizationScopes`), carried only by the operations token.** For the chat
  SRP sign-in to carry a custom scope on its **access** token would require a
  pre-token-generation Lambda trigger (V2) that customizes the access token.
  Access-token customization is a Cognito **Plus feature-plan** capability and
  bills per monthly active user at the Plus tier, so it raises the user-pool
  cost. It is also the wrong shape here: the goal is that the chat token
  **cannot** carry the operations entitlement, which a separate audience
  expresses directly without a Lambda. Rejected as the primary mechanism for
  cost and because audience separation is a cleaner invariant.
- **Accept the residual and compensate.** Keep one audience and rely on: the raw
  token never being exposed to tools or the model, no generic authenticated-HTTP
  tool existing in the runtime, and restricted runtime egress. Rejected: these
  are runtime-code promises, not an authorization boundary, and the issue
  requires that chat gain no operations credential. Recorded here so a maintainer
  who prefers it can adopt it explicitly, in which case the ADR, the threat
  model O9/OP-H1/OP-H7, and the version history must say "accepted residual" and
  list those compensating controls instead of "closed".

Until decision 1 is implemented, the chat runtime **does** hold a bearer token
the operations API accepts. This record and the threat model state that plainly
and track it as OP-H7 with a live exercise (a token captured at the AgentCore
boundary is rejected (401) by every operations route).

### 2. The trusted frontend server proxy submits the proposal

A new server-side proxy in the frontend (the same trusted tier that will hold
the #554 approve and reject proxies and the operator-console proxies) is the
only component that submits a proposal to `POST /operations/prepare`. It does so
on behalf of the verified user, carrying the **operations-audience** access
token from decision 1 in the `Authorization` header. The token is read from the
server-side operations cookie, never from the browser, a request body, or a
custom header.

The chat runtime does not call the operations API. The AgentCore chat role gains
no operations permission and no provider-write permission. The handoff crosses
from the browser to the frontend server, and from the frontend server to the
operations API; it never flows through the AgentCore runtime, a chat tool, or a
model tool call. #429's property — preparation is unreachable from the chatbot —
continues to hold for the chat runtime until #441 activation.

The proxy derives the requester identity from the verified operations access
token and reads `cognito:groups` from that **access** token (not an ID token),
consistent with [Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity).
It requires the server-owned proposer group `operations-proposer` (decision 3),
enforces same-origin writes, rejects redirects, bounds response bytes, and
rate-limits per subject. The proxy group check is UI defense in depth; the
authoritative proposer decision is server-owned in the application service
(decision 3).

### 3. Model output becomes a typed, untrusted proposal through a fenced contract

Model output is proposal data, never executable content and never authority.
The handoff from a described capacity change to a typed proposal uses a
versioned, fail-closed fenced contract, mirroring
[the inline chart contract](../CHART_CONTRACT.md):

- **Transport.** When the assistant has described a specific capacity change, it
  emits a fenced `proposal` block whose body is a single JSON object carrying
  **only** `capability_id` (the constant `gamelift.capacity-adjustment`),
  `fleet_id`, `location`, and the requested `{desired, minimum, maximum}` triple,
  with a `version` field and `additionalProperties: false`. The frontend
  validates it against the shared bounds and **fails closed**: anything out of
  contract renders as inert text, exactly as an invalid chart fence does. This
  is a new **chat-UI** contract (`proposal` fence) plus a prompt directive
  telling the model it may describe and propose but never submits, approves, or
  invokes a change itself. Per
  [Repository Guidance](../../AGENTS.md), the prompt directive change is not
  deployed until it is published through `scripts/infrastructure/deploy-prompts.sh`
  or the full deployment.
- **The proxy observes first; `observation_id` is never model-authored.** The
  chat path creates no E1 observation, so no trusted `observation_id` exists in
  the conversation. The proposal proxy therefore calls `POST /operations/observe`
  for the confirmed target fleet and location, obtains a succeeded, unexpired,
  workspace-scoped observation, and uses **that** `observation_id`. Neither
  `observation_id` nor the idempotency token (decision 4) is ever taken from
  model output or a request-body field.
- **One explicit user gesture, no auto-submit.** Trusted UI code renders a
  confirmation card from the fence values **and** the trusted current state from
  the observe call. Fields are editable. The proposal is submitted only on one
  explicit user gesture; nothing submits on render.
- **Rendering the outcome.** On a successful preparation the proxy returns the
  `operation_id` and reads the preview from `GET /operations/{operationId}`
  (decision 5). A deterministically `denied` outcome (HTTP 200,
  `persisted: false`, not stored, so the evidence read returns 404) is rendered
  as a refusal with no deep link.

The proxy maps the confirmed fields to the versioned
`gamelift-capacity-proposal-request` body plus the proxy-obtained
`observation_id` (decision 1 of M1 below). The `PrepareOrchestrator` enforces
the exact key set, recomputes advice from the trusted current-state read,
selects the playbook in code, binds identity from the verified principal, and
stores one immutable prepared operation with its canonical hash. Free text never
becomes an executable field, and the model never selects a capability, lowers
risk, or grants authority.

A fabricated or injected proposal — the model, or injected provider content,
proposing a change the user never asked for — is therefore bounded: the user
must perform one explicit gesture, the target current state is server-observed
and shown, the server-owned capacity band bounds the magnitude, a distinct
`admin` must approve, the preparation expires (default 900 s), and the requester
can cancel. No provider write happens before E3 dispatch.

### 4. A server-owned proposer policy and server-side submission limits

Proposer authorization and submission limits are authoritative **in the E2
application service**, not only in the proxy, so a direct caller of the public
operations API cannot bypass them. This is an E2 behavior change filed as a
child issue:

- **Proposer policy.** The application service admits a prepare only from a
  requester in the server-owned `operations-proposer` group (or the policy a
  maintainer chooses). The proxy's group check is defense in depth; the service
  check is the boundary, consistent with
  [ADR 0002](0002-use-protocol-neutral-operations-services.md)'s rule that
  adapters never own authorization.
- **Per-requester cap.** The service caps the number of concurrent
  `pending_approval` operations per requester, so a single subject cannot flood
  preparation.
- **Separate prepare throttle.** `POST /operations/prepare` gets its own
  route-level throttle, distinct from the shared approval-route throttle, so a
  prepare flood cannot starve approve, reject, and cancel. WAF does not apply to
  HTTP APIs and the frontend per-task limiter is per ECS task, so neither is the
  authoritative limit.

### 5. The user reaches approval through the direct operator approval view

Approval is a direct, authenticated UI or API action outside the chat and model
tool path. After a successful preparation the proxy returns the `operation_id`
and reads the stored preview from `GET /operations/{operationId}`, whose
response carries `{operation_contract_version, operation_id, decision,
prepared_hash, persisted, replayed}` and the bounded evidence record (there is
no preview field on the prepare response itself). Trusted UI code deep-links the
user to the operator approval view planned in
[#554](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/554),
which loads the preview from the immutable record by ID and renders the action,
exact parameters, risk, requester, and expiry, then offers approve and reject
actions that call the #554 server-side proxies for the E2 approve and reject
routes. Approve carries only the path `operation_id`; reject and cancel carry at
most an optional `expected_prepared_hash`.

An optional in-chat affordance, in the spirit of Cloudscape's
["user-authorized actions"](https://cloudscape.design/gen-ai/patterns/user-authorized-actions/)
pattern, is **navigation-only**: selecting it navigates the user to the #554
approval view. It takes `operation_id` only from the proxy's prepare response,
never from a model-authored link or chat text. There is **no** approve or reject
control inside the chat transcript. The approve or reject decision never travels
inside a chat message, a model response, a model tool call, or any request body
beyond the path ID and optional `expected_prepared_hash`. The in-chat affordance
is additive and gated behind the same default-off operations UI gate as the
operator console; the #554 approval view is the authoritative surface. The #554
proxies do not exist yet; #554 is open and blocked by #430, #437, and #546, and
the current operations infrastructure provides only a cancel action proxy.

### 6. Identity, tenant, and workspace come only from verified server-side context

The proposal proxy and the operations API derive requester identity from the
verified operations access token, and tenant and workspace from the server-owned
bindings. The frontend chat path binds `GBAW_TENANT_ID` and `GBAW_WORKSPACE_ID`;
the operations API binds `GBAW_OPERATIONS_TENANT_ID` and
`GBAW_OPERATIONS_WORKSPACE_ID`. The proposal proxy sends **no** tenant or
workspace in the body; the operations API resolves both from its own bindings.
`validate-deployment.sh` should check that the chat and operations tenant and
workspace bindings are equal in a single-workspace deployment, or the record
must state that they are intentionally independent. The operations API reads
identity solely from its API Gateway JWT authorizer context, never from the
body, query string, or a custom header. Request-body fields, CopilotKit data,
chat text, model output, tool arguments, and browser-supplied identity cannot
establish or override requester, approver, tenant, workspace, group, or any
authorization value. The approver is a separate verified principal supplied to
the approve route, and separation of duties follows the server-owned policy in
[Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity).

### 7. When operations are disabled or absent, the chat path stays read-only

The frontend discovers operations availability from the existing
`operations-capability-discovery` 1.0 contract, which exposes per-capability
`enabled`, `effective_authority`, and `phases.prepare`. Trusted UI code renders
the proposal and in-chat affordance only when
`capabilities["gamelift.capacity-adjustment"].phases.prepare === true`.
Discovery is served from the control-plane base (`GBAW_OPERATIONS_API_BASE_URL`)
through an admin-only proxy; an E2-only (`advise`) deployment without the E4
discovery control plane therefore never renders the affordance, which is the
intended fail-closed dependency. A trusted UI notice — not model output — tells
the user that the assistant can describe but not start a change, and the prompt
directive (decision 3) tells the model it never starts a change itself; the
runtime is not told operations availability and should not be. A hidden or stale
UI control is never authorization: the operations API fails closed independently
when its runtime mode is below the capability's required authority
(the `advise`/mode check) or the kill-switch prepare phase is engaged, and a
provisioned-but-disabled stack throttles its API stage to zero.

### 8. Submission idempotency

The trusted confirmation component mints a CSPRNG idempotency token
(`idem_` followed by 20–128 URL-safe characters, matching
`^idem_[A-Za-z0-9_-]{20,128}$`, with at least 128 bits of entropy) when the
confirmation card is created, and the proxy forwards it unchanged; a transport
retry reuses the same token. The token is never derived from proposal fields or
model output. The operations API enforces workspace-scoped idempotency: a
byte-identical retry under the same token replays the stored operation, and a
changed intent under the same token fails with `IDEMPOTENCY_CONFLICT`
([Operations Contracts](../OPERATIONS_CONTRACTS.md#workflow-identity-and-idempotency)).
Because the intent fingerprint and the idempotency key do not bind the
requester, two different requesters submitting an identical proposal under the
same token in a shared workspace would replay one operation; that cross-requester
replay under a known token is recorded as a residual. Preparation touches no
provider, so a submission produces at most one immutable `pending_approval`
record awaiting a direct human approval, never a provider write.

### 9. Audit

The operations application services write an append-only ledger event per
**persisted** operation, keyed by `operation_id`. For a persisted
`approval_required` operation the ledger records the `operation.prepared` event,
the requester's `trusted_identity`, and the correlation `request_id`, and later
the approve or reject events. A **deterministically denied** operation is never
persisted, and a contract-invalid body creates no operation; both leave only the
`PreparationFailures` metric and bounded, redacted logs, so not every submission
produces a ledger event. The ledger stores verified identities (opaque subject
and client identifiers), never an email address, display name, or a body-supplied
"source" field; raw tokens and cookies are never logged
([Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#logging)).

Chat-origin attribution is **not** inherently recorded: a persisted operation
records `recommendation.source_type` from the fixed enum `human|model|automation`,
which does not distinguish a chat-originated, human-confirmed proposal from a
console one. The chosen attribution signal is the distinct operations app
`client_id` from decision 1, which lands in `requester.client_id` on the
operation; a chat-originated submission therefore carries the operations client
rather than any body field. Changing `recommendation.source_type` semantics
would require a new contract version and is not adopted here.

## Preserved Invariants

- The chat runtime never gains a provider-write permission, and the AgentCore
  chat role stays provider-read-only ([ADR 0003](0003-isolate-provider-writes.md)).
  The chat runtime gains no operations credential **once decision 1
  (token-audience separation) is implemented**; until then it holds a bearer
  token the operations API accepts, tracked as OP-H7.
- Approval never travels through a chat message, a model response, a model tool
  call, or any request body beyond the path ID and optional
  `expected_prepared_hash`. It is a direct authenticated UI or API action on the
  E2 approve route ([Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity)).
- Model output is untrusted proposal data. It never selects a capability,
  grants authority, lowers risk, or supplies `observation_id` or the idempotency
  token; preparation validates and binds everything
  ([ADR 0002](0002-use-protocol-neutral-operations-services.md)).
- The default deployment remains read-only and provisions no operations
  resource ([ADR 0001](0001-preserve-chat-and-add-optional-operations.md)).

## Relation to ADR 0002 and #429

[ADR 0002](0002-use-protocol-neutral-operations-services.md)'s accepted diagram
routes a chat adapter to the application services. This record creates **no**
chat adapter for prepare: the path is Operations UI → trusted Next.js proxy →
operations HTTP API, and the chat runtime is off it. #429's property that
preparation "remains unreachable from the chatbot" is preserved for the chat
runtime, and remains so until #441 activation. ADR 0002's single-authorization
rule is honored because the authoritative proposer policy and limits live in the
E2 application service (decision 4), not in the proxy adapter.

## Contract Impact

- **Unchanged.** The source-control `prepare-operation-request` 1.0 schema is
  **not** touched by this record. The E2 capacity prepare body is the separate
  `gamelift-capacity-proposal-request` 1.0 schema
  (`urn:game-agent:operations:contracts:v1:gamelift-capacity-proposal-request`):
  `request_contract_version`, `capability_id = gamelift.capacity-adjustment`, a
  client-supplied `idempotency_token`, and `proposal{fleet_id, location,
  requested{desired, minimum, maximum}}`, with `additionalProperties: false`
  everywhere. `observation_id` is **not** in that published schema; the
  `PrepareOrchestrator` requires the body to be exactly that schema **plus**
  `observation_id` and rejects any other key. The code key-set check is the
  boundary; a maintainer may optionally publish a strict prepare-envelope schema
  with its own `$id` as an additive item.
- **New (chat UI).** A versioned, fail-closed `proposal` fence contract
  (decision 3), validated in the frontend and rendered inert on any mismatch,
  plus a prompt directive published through `deploy-prompts.sh`.
- **Reused.** The existing `operations-capability-discovery` 1.0 contract gates
  the affordance on `phases.prepare` (decision 7). Any extension is a new version
  of that contract (a new `$id`) under
  [Operations Contracts](../OPERATIONS_CONTRACTS.md#compatibility-and-publication),
  not a new contract.

## Implementation

The acceptance criterion that implementation is split into focused child issues
with blockers is satisfied by the children below. The ADR is Proposed; a
maintainer accepts it and files these as issues, so the PR is **Part of #555**,
not Closes.

1. **Operations token-audience separation (blocker of E2 activation #441).**
   Second Cognito app client for the operations API; E2/E1 authorizer audience
   lists only that client; server-side authorization-code + PKCE exchange and
   `HttpOnly` operations cookie; AgentCore authorizer keeps only the chat client.
   Blockers: base-stack app-client and managed-login domain; #441.
2. **Proposal proxy.** Server-side submit-to-prepare proxy that observes first,
   forwards the operations token and the CSPRNG idempotency token, enforces
   same-origin and the proposer-group defense-in-depth check.
   Blockers: child 1; the E4 admin-only discovery proxy; #554 proxy tier parity.
3. **Proposal fence and confirmation card.** The versioned `proposal` fence,
   its shared frontend validator (fail closed), the confirmation card showing
   server-observed current state, one-gesture submit, and `denied` rendering.
   Blockers: child 2; the prompt directive via `deploy-prompts.sh`.
4. **Server-owned proposer policy and limits.** `operations-proposer` policy in
   the E2 application service, per-requester concurrent `pending_approval` cap,
   and a dedicated `POST /operations/prepare` route throttle (E2 behavior change
   with tests). Blockers: #414.
5. **Navigation-only in-chat entry.** Presentational affordance gated on
   discovery `phases.prepare`, navigating to the #554 approval view; the chat
   renderer never renders raw HTML from model output. Blockers: child 3; #554.
6. **Direct approval surface (#554).** Open; blocked by #430, #437, and #546.
7. **UI shell integration (#546).** The in-chat entry survives the Cloudscape
   shell change without changing the trust boundary.
8. **Chat-reachability guard (#429/#430).** Keep preparation unreachable from
   the chat runtime; regression that the chat role has no operations token after
   child 1.
9. **Autonomy and remote boundaries (#416/#437).** No change to this record;
   listed so later phases do not reintroduce a chat-reachable prepare path.

## Consequences

- Closing the shared-token exposure (decision 1) adds a second Cognito app
  client, a managed-login domain, and a server-side token exchange, and also
  closes the equivalent residual on the E1 read-only observation API.
- The handoff reuses the trusted frontend proxy tier and the #554 approval view,
  so no new service and no new trust boundary type is introduced; a single new
  trust boundary (chat proposal submission) is added to the threat model.
- The chat assistant can turn observed state into a reviewable proposal without
  ever holding write or approval authority once decision 1 lands, and the user
  approves on a surface the model cannot reach.
- The in-chat affordance is navigation-only, so the authoritative approval
  surface stays the #554 operator view and a future UI shell change
  ([#546](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/546))
  does not change the trust boundary.
- A disabled or unprovisioned operations stack leaves the chat path read-only
  with a trusted UI notice, and the API fails closed regardless of any UI state.
- The frontend gains a proposal proxy that must be reviewed to the same bar as
  the #554 approve and reject proxies.

## Rejected Alternatives

- **Let the chat runtime call the operations prepare API.** Rejected: it would
  require giving the provider-read-only AgentCore role an operations credential
  and a network path to the write-adjacent control plane, expanding the chat
  runtime's blast radius and violating [ADR 0003](0003-isolate-provider-writes.md).
  Model output or a prompt injection could then drive preparation directly.
- **Build a new standalone proposal-submission service.** Rejected: it would
  duplicate the trusted-proxy authentication, group check, and token handling the
  #554 proxy tier will provide, creating a second code path to keep in parity
  ([ADR 0002](0002-use-protocol-neutral-operations-services.md)) for no
  capability the proxy tier lacks.
- **Render the approve action inside the chat transcript (an in-transcript
  approve card calling the approve proxy).** Rejected: it would place the approve
  control next to model-authored text that can mislead the approver, amplifying
  the OP-CD2 indirect-injection residual, on an origin whose CSP permits inline
  script. `SameSite=Lax` and the same-origin check do not stop a same-origin
  script. The in-chat entry is navigation-only instead.
- **Call the approve route from a model tool.** Rejected: approval would then
  travel through model output and a model tool call, which the approval boundary
  forbids ([Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md#approval-identity)).
- **Have the model emit the typed prepare body directly and submit it unchanged.**
  Rejected: the body would be untrusted model output. The fenced `proposal`
  contract carries only bounded intent, the proxy observes current state itself,
  the user confirms, and preparation re-derives and validates everything, so
  model-authored structure is never trusted as the proposal.
- **Add identity, tenant, or workspace fields to the proposal body so chat can
  pass context.** Rejected: it would create a body-supplied-identity bypass.
  These values are server-owned
  ([Operations Contracts](../OPERATIONS_CONTRACTS.md#trusted-context)).
- **A phase-gated MCP / AgentCore Gateway prepare tool for the chat path**
  (as [ADR 0004](0004-expose-governed-public-mcp-facade.md) contemplates for
  remote clients). Rejected for the chat path: it would reintroduce a
  chat-reachable prepare tool and a model-invocable path to preparation, which
  #429 forbids. A governed public MCP facade remains a separate, remote-client
  decision under ADR 0004, not the chat handoff.
- **A required custom access-token scope on the operations routes.** Rejected as
  the primary mechanism (recorded under decision 1): carrying a custom scope on
  the SRP access token needs a pre-token-generation Lambda V2 access-token
  customization, a Cognito Plus feature-plan capability billed per MAU, and
  audience separation expresses the "chat token cannot carry it" invariant more
  directly.
