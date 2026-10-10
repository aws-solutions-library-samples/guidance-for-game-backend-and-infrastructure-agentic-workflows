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
context). As built on the E1/E2 branches, every operations HTTP API authorizer
is configured to accept the **same** Cognito app-client audience. The AgentCore
chat role holds no IAM permission for the operations stack, but a forwarded
bearer token is itself a usable credential: runtime code that can make an
authenticated HTTP call could replay that token against
`POST /operations/prepare` and `/cancel`, and for an `admin` user against
`/approve` and `/reject` on another requester's pending operation. As the E3 and
E4 branches add the dispatch and control-plane APIs under the same shared
audience, that same forwarded token would also reach
`POST /operations/{operationId}/dispatch` and `POST /operations/control` (the
kill switch) for an admin. IAM separation does not close this; token-audience
separation does.

Decision 1 therefore makes token-audience separation a **blocker** of E2
proposal activation (#441), and this record stops asserting that the chat
runtime holds "no credential" until that separation is implemented. The same
residual already applies to the read-only observation routes as built on the
E1/E2 branches, because they share the same authorizer audience, and the same
mechanism closes all of them.

## Decision

### 1. The operations API must accept only a token the chat runtime never receives

**Every** operations HTTP API MUST authorize only a bearer token whose audience
the chat-forwarded token cannot carry. The operations stack is three separate
API Gateway HTTP APIs, each with its own Cognito JWT authorizer, and the chat
token is accepted by all three:

- the E1/E2 action API — observe, prepare, approve, reject, cancel, and the
  evidence read;
- the E3 dispatch API — `POST /operations/{operationId}/dispatch`, an admin
  action that starts the approved workflow;
- the E4 control-plane API — capabilities, list, detail, and
  `POST /operations/control` (the kill switch), whose handler additionally
  requires the token's `client_id` to equal the authorizer's trusted audience.

Each authorizer's audience and each handler's trusted audience
(`GBAW_OPERATIONS_TRUSTED_AUDIENCE`) default to the single `CognitoClientId` the
operator supplies, so a chat-forwarded admin token reaches dispatch and the kill
switch as well as prepare. This decision is a **blocker of E2 proposal
activation (#441)** and also closes the equivalent residual on the read-only
observation routes, which share the same authorizer audience.

**Chosen mechanism — a separate, confidential operations Cognito app client
(audience) whose token the frontend server obtains without the operations
client's own credentials from within a managed-login session.**

- A second Cognito app client is provisioned for the operations API. **Every**
  operations JWT authorizer (E1/E2 action, E3 dispatch, E4 control plane) lists
  **only** that operations client in its `Audience`, and every handler that
  checks a trusted audience (the E4 control handler) is given the operations
  client. The AgentCore authorizer keeps **only** the existing chat client. A
  token minted for the chat client is rejected (401) by every operations route —
  action, dispatch, and control — and a token minted for the operations client
  is never forwarded to the AgentCore runtime.
- The `deploy-operations-observation`, `deploy-operations-execution`, and
  `deploy-operations-control-plane` shell scripts and their PowerShell
  counterparts take the authorizer audience and the trusted audience from
  operator input (`CognitoClientId`); they MUST be given the operations client
  ID, not the chat client ID.
- **Every** operator-console proxy moves to the operations token. The
  console proxies that today forward the chat client's access token
  ([`ui/src/operations/proxyAuth.ts`](../../ui/src/operations/proxyAuth.ts))
  — including the existing cancel proxy and the capability-discovery proxy —
  read the server-side operations cookie instead. Moving only the E1/E2
  authorizer would break the existing console cancel proxy, which forwards the
  chat token.
- **Obtaining the operations token (chosen path (b)): move the application's
  primary sign-in to Cognito managed login.** Cognito's `/oauth2/authorize`
  endpoint is an interactive browser redirect, and its managed-login session
  cookie is set only when the user signs in through the login pages; a refresh
  token works only with the client that issued it. An SRP sign-in through
  `amazon-cognito-identity-js` creates no managed-login session, so no token for
  a second client can be obtained from it silently. The application therefore
  moves its primary sign-in from in-browser SRP to managed login using the
  authorization-code grant with PKCE for the **chat** client, completed by the
  frontend server. Within that one-hour managed-login session the server then
  runs an authorization-code + PKCE exchange for the separate **operations**
  client (using `prompt=none` where the managed-login branding version supports
  it, so no second credential prompt is shown), and keeps the operations access
  and refresh tokens server-side in `HttpOnly`, `Secure`, `SameSite=Lax` cookies
  scoped to the operations proxies. Neither client's tokens reach browser
  JavaScript.
- **Consequences of path (b).** It requires the managed-login branding version
  (available on the Essentials plan, which the base user pool already defaults
  to), a managed-login domain, callback and logout routes, and replaces
  `CognitoAuth.tsx` and the `/api/auth/login` and `/api/auth/refresh` token
  posting. Logout must call Cognito `/logout`. It must be coordinated with the
  Cloudscape app-frame work
  ([#548](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/548)),
  which rebuilds the sign-in experience. As a side benefit, both clients' tokens
  stay out of browser JavaScript.
- **Acceptable interim (path (a)).** If activation (#441) precedes the sign-in
  change, the operations client may instead use a step-up interactive sign-in at
  first operations use: the browser is redirected to managed login for the
  operations client, the user signs in there, and the frontend server keeps the
  operations client's own refresh token server-side in an `HttpOnly` cookie to
  mint subsequent operations access tokens. This works with the hosted login on
  the pool's default plan.
- **For whichever path runs.** The operations refresh token is bound to the
  operations client, stored `HttpOnly`, and revoked on logout. The OAuth `state`
  value and the PKCE verifier are bound to the existing session. The operations
  proxies require the operations token's `sub` to equal the verified chat
  session's `sub`, so a step-up sign-in cannot authorize as a different account.
- **The operations app client is confidential.** Its secret lives only on the
  frontend server, and its allowed OAuth flows are limited to authorization code
  and refresh. A public operations client would let any pool user run the
  authorize flow in their own browser and redeem the code with their own PKCE
  verifier, which would make `requester.client_id` mean "some holder of an
  operations token" rather than "the trusted proposal proxy"; a confidential
  client is what makes `client_id` attribution (decision 9) meaningful.
- **Trade-offs.** Path (b) adds a managed-login domain, a second confidential
  app client, callback and logout routes, and replaces the SRP sign-in UI; it
  removes both clients' tokens from the browser. Path (a) is smaller but leaves
  the chat tokens in browser storage and shows a second sign-in. Both are
  reversible: removing the operations client and its proxies returns the
  deployment to chat-only.

**Alternatives (recorded, not chosen):**

- **A required custom OAuth scope on the operations routes
  (`AuthorizationScopes`), carried only by the operations token.** Tokens issued
  through `InitiateAuth` (the SRP sign-in) never carry custom scopes, so the
  chat token already cannot carry an operations scope; the hard part is issuing a
  token that **does** carry one, which needs the same move to a managed-login
  authorization-code flow as audience separation. Carrying a custom claim on the
  access token would also require a pre-token-generation Lambda trigger (V2);
  access-token customization needs the Essentials or Plus feature plan, and the
  base pool already defaults to Essentials, so this adds no plan change — but it
  is the wrong shape, because the goal is that the chat token **cannot** carry
  the operations entitlement, which a separate audience expresses directly
  without a Lambda. Rejected because audience separation is a cleaner invariant.
- **A second SRP authentication against the operations client in the browser
  (path (c)).** Rejected: the operations tokens would then pass through browser
  JavaScript and the library's `localStorage` cache, contradicting this record's
  property that neither client's tokens reach the browser.
- **Accept the residual and compensate.** Keep one audience and rely on: the raw
  token never being exposed to tools or the model, no generic authenticated-HTTP
  tool existing in the runtime, and restricted runtime egress. Rejected: these
  are runtime-code promises, not an authorization boundary, and the issue
  requires that chat gain no operations credential. Recorded here so a maintainer
  who prefers it can adopt it explicitly, in which case the ADR, the threat
  model O9/OP-H1/OP-H7, and the version history must say "accepted residual" and
  list those compensating controls instead of "closed".

Until decision 1 is implemented, the chat runtime **does** hold a bearer token
every operations API accepts. This record and the threat model state that
plainly and track it as OP-H7 with a live exercise (a token captured at the
AgentCore boundary is rejected (401) by every operations route — action,
dispatch, and control).

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
It requires the server-owned proposer group `operations-proposer` (decision 4),
enforces same-origin writes, rejects redirects, bounds response bytes, and
rate-limits per subject. The proxy group check is UI defense in depth; the
authoritative proposer decision is server-owned in the application service
(decision 4).

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
  `observation_id` nor the idempotency token (decision 8) is ever taken from
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
`observation_id` (see Contract Impact). The `PrepareOrchestrator` enforces
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
response carries `{evidence_contract_version, operation_id, state, prepared_hash,
preview, approval, ledger, handoff}`, where `preview` holds the action, target,
requested triple, risk, authority, and expiry (the prepare response itself has
no preview field; its fields are `{operation_contract_version, operation_id,
decision, prepared_hash, persisted, replayed}`). Trusted UI code deep-links the
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
`validate-deployment.sh` checks that the chat and operations tenant and
workspace bindings are equal in a single-workspace deployment. The operations
API reads
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
the proposal and in-chat affordance only when the discovery `capabilities`
array contains an entry whose `capability_id` is `gamelift.capacity-adjustment`
with `phases.prepare === true`.
Discovery is served from the control-plane base (`GBAW_OPERATIONS_API_BASE_URL`)
through a proposer-scoped proxy: the `operations-proposer` group may read
capability discovery (not only `admin`), so a non-admin proposer sees the
affordance for which it is authorized. The proposer-scoped discovery proxy is
part of the proposal-proxy child issue (child 2). An E2-only (`advise`)
deployment without the E4 discovery control plane therefore never renders the
affordance, which is the intended fail-closed dependency. A trusted UI notice — not model output — tells
the user that the assistant can describe but not start a change, and the prompt
directive (decision 3) tells the model it never starts a change itself; the
runtime is not told operations availability and should not be. A hidden or stale
UI control is never authorization: the operations API fails closed independently
when its runtime mode is below the capability's required authority
(the `advise`/mode check) or the kill-switch prepare phase is engaged, and a
provisioned-but-disabled stack throttles its API stage to zero.

### 8. Submission idempotency

The trusted confirmation component mints a CSPRNG idempotency token
(`idem_` followed by at least 22 URL-safe characters, within the contract
pattern `^idem_[A-Za-z0-9_-]{20,128}$`) when the confirmation card is created,
and the proxy forwards it unchanged; a transport retry reuses the same token. At
least 22 base64url characters are minted because 20 give only about 120 bits,
below the 128-bit floor. The token is never derived from proposal fields or
model output. The confirmation component mints a **new** token when the user
edits the proposed fields after a definitive failure, so an edited intent is not
submitted under a token already bound to the earlier intent. The operations API
enforces workspace-scoped idempotency: a
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
rather than any body field. This signal is meaningful only because the
operations app client is **confidential** (decision 1): a public client would
let any pool user mint an operations token in their own browser, so
`requester.client_id` would mean "some holder of an operations token" rather
than "the trusted proposal proxy". Changing `recommendation.source_type`
semantics would require a new contract version and is not adopted here.

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

1. **Operations token-audience separation (blocks E2 activation #441).** A
   second, **confidential** Cognito app client for the operations API; **every**
   operations JWT authorizer (E1/E2 action, E3 dispatch, E4 control plane) and
   every handler trusted audience (`GBAW_OPERATIONS_TRUSTED_AUDIENCE`, the E4
   control handler) lists only that client; the `deploy-operations-observation`,
   `deploy-operations-execution`, and `deploy-operations-control-plane` shell and
   PowerShell wrappers that take `CognitoClientId` are given the operations
   client ID; every operator-console proxy (including the existing cancel proxy
   and the discovery proxy) moves to the server-side operations token; the
   AgentCore authorizer keeps only the chat client. Primary sign-in moves to
   Cognito managed login (authorization-code + PKCE for the chat client), and the
   frontend server obtains the operations client's token within that session
   (`prompt=none` where available), keeping both tokens off browser JavaScript;
   the operations refresh token is client-bound, `HttpOnly`, revoked on logout,
   and the proxy requires the operations token's `sub` to equal the chat
   session's `sub`. Blockers: a base-stack confidential operations app client, a
   managed-login domain, callback and logout routes, the managed-login branding
   version (Essentials default), and coordination with the Cloudscape sign-in
   rebuild (#548). This child blocks #441; it is not blocked by it.
2. **Proposal proxy.** Server-side submit-to-prepare proxy that observes first,
   forwards the operations token and the CSPRNG idempotency token, enforces
   same-origin and the proposer-group defense-in-depth check; includes the
   proposer-scoped capability-discovery proxy that lets the `operations-proposer`
   group read discovery.
   Blockers: child 1; #554 proxy tier parity.
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

- Closing the shared-token exposure (decision 1) adds a second confidential
  Cognito app client, a managed-login domain, callback and logout routes, moves
  the primary sign-in to managed login (coordinated with #548), moves every
  operator-console proxy to the operations token, and covers all three
  operations authorizers (action, dispatch, control). It also closes the
  equivalent residual on the read-only observation routes.
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
  the primary mechanism (recorded under decision 1): tokens from the SRP
  `InitiateAuth` sign-in never carry custom scopes, carrying a custom claim on
  the access token needs a pre-token-generation Lambda V2 (available on the
  Essentials or Plus feature plan, which the base pool already defaults to), and
  audience separation expresses the "chat token cannot carry it" invariant more
  directly.
