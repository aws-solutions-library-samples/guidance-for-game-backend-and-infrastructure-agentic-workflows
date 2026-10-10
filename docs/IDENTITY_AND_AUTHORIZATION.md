# Identity and Authorization

This document defines the trusted identity boundary for the current
observation path and for the optional operations control plane. It does not
enable operations or change the customer sign-in experience.

## Current Web Path

The production request path is:

```text
Cognito access token
  -> Next.js API adapter
  -> immutable principal context
  -> bearer-token AgentCore invocation (Cognito access token)
  -> sanitized backend request context
```

The API adapter verifies access and ID tokens with separate
`aws-jwt-verify` verifiers. The access verifier enforces the user pool,
application client, signature, expiration, and `token_use=access`. The ID
token is separately verified and bound to the access token by `sub`.

Authorization uses only the verified access token. The ID token supplies
presentation data such as email to the UI; its groups do not grant backend
authority.

The current Cognito sign-in flow is unchanged. Observation authorization uses
the access token's `cognito:groups` claim, so a custom OAuth scope migration is
not required.

## Trusted Principal

The adapter constructs this immutable context:

| Field | Trusted source | Rule |
|---|---|---|
| `user_id` | Access-token `sub` | Required; identifies the human subject |
| `client_id` | Access-token `client_id` | Must exactly match `COGNITO_CLIENT_ID` |
| `audience` | Access-token `aud`, otherwise `client_id` | Must match `GBAW_COGNITO_AUDIENCE`; the current deployment binds it to the application client |
| `groups` | Access-token `cognito:groups` | Array of nonempty strings; absent becomes an empty array |
| `scopes` | Access-token `scope` | Space-delimited scopes; absent becomes an empty array |
| `tenant` | Server environment `GBAW_TENANT_ID` | Required deployment binding |
| `workspace` | Server environment `GBAW_WORKSPACE_ID` | Required deployment binding |

Subject and client are distinct even when both participate in an authorization
decision. Tenant and workspace are initial single-deployment bindings, not a
browser selection or a new workspace registry.

The adapter rejects malformed or mismatched claims. It returns service
unavailable when the trusted tenant/workspace binding is missing. Request
bodies, CopilotKit data, chat text, model output, and tool arguments cannot
override principal fields.

The backend sanitizer allowlists the same fields and exposes them at the top
level of the agent request context. The minimum source-control read contract is
`user_id`, `groups`, `tenant`, and `workspace`. Read authorization must use
that context and the existing connector policy; it must not create another
workspace model or accept identity through tool input.

## Approval Identity

The protocol-neutral approval boundary in `operations.identity` and
`operations.approval` now enforces these separate values:

- `requester`: copied from a fresh verified principal when the immutable
  operation is prepared;
- `approver`: copied from the fresh verified principal supplied separately for
  the direct approval action; and
- `prepared_operation_hash`: freshly calculated from the validated stored
  operation and recorded with the approval.

An approval action remains a direct authenticated UI or API action outside the
chat and model tool path. Its complete untrusted payload contains only an
operation identifier. `ApprovalService` loads the stored document and hash,
checks its contract, `pending_approval` state, expiry, policy version, and
requester boundary, then constructs the approval record from verified context.
Chat text and model output can request preparation but cannot invoke or
synthesize approval.

`VerifiedPrincipal` may be constructed only by verifier-owned adapter code
after cryptographic access-token verification. It is a trusted capability, not
a JSON data-transfer object, and must never be registered for generic request
deserialization. The application boundary checks credential freshness and
trusted audience, client, tenant, and workspace configuration; it never
accepts a token or principal mapping from the approval payload.

The service rechecks credential and operation expiry immediately before commit.
The approval store receives expected state and hash plus an exclusive
`commit_not_after` deadline equal to the earliest credential, operation, or
approval-record expiry, and returns a typed recorded, precondition-failed, or
deadline-expired outcome. Only exact outcome enum members are accepted; raw
string values fail closed. Its durable implementation must enforce all three
conditions in the same atomic transaction that records the approval and state
transition.

The default policy denies self-approval. A tenant/workspace policy may
explicitly allow the same principal to approve an eligible low-risk operation.
Higher-risk operations require a different authorized principal. The
operations implementation must cover at least these policy cases:

| Requester and approver | Explicit low-risk self-approval policy | Result |
|---|---:|---|
| Different | Either | Continue with approver authorization checks |
| Same | Disabled or absent | Deny |
| Same | Enabled and operation is eligible | Allow one approval for the stored hash |
| Same | Enabled but operation is ineligible | Deny |

Reusing an approval for changed content, another hash, another requester,
another tenant/workspace, or an expired operation is denied. Versioned
prepared-operation and approval schemas are defined by issue
[#277](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/277).

## Remote MCP Clients

The preferred remote MCP path uses OAuth 2.0 or OIDC bearer tokens. Its HTTP
adapter must verify issuer, signature, expiration, token use, audience, and
client before constructing the same principal context. Tenant/workspace may
come from verified claims or a trusted client-registration mapping, never from
MCP tool arguments.

If a remote adapter cannot establish subject or service identity, client,
audience, tenant, and workspace from trusted sources, it must not expose
operations. SigV4 service clients require a separate authenticated service
principal path and must not impersonate a Cognito user.

There is no remote MCP HTTP adapter in the current deployment. End-to-end
OAuth/OIDC propagation testing therefore belongs with that adapter; the
fallback today is to keep remote operations unavailable.

## Executor Identity

The durable workflow and prepared executor use an authenticated service
identity distinct from requester and approver. The workflow sends only an
operation identifier. The identifier is not a credential.

Before any provider action, the executor must authenticate and authorize the
workflow caller, load the operation from trusted storage, and verify the stored
hash, approval, tenant/workspace, contract version, and executor binding. It
must reject direct user, chat-runtime, model, and unauthenticated invocations.
Issue [#314](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/314)
owns that implementation.

## Logging

Application logs record request/correlation IDs, bounded metrics, and
operation outcomes only. They do not contain prompt text, extracted names,
semantic-memory content, email addresses, display names, raw Cognito subjects,
or raw session/thread identifiers.

- Agents run without the default Strands stdout printer
  (`callback_handler=None`), so streamed model text and tool names are not
  written to stdout, which the hosted runtime ships to its log group. A static
  test checks every `Agent(...)` construction.
- Correlation uses a per-request ID. The backend binds the AgentCore runtime
  request ID into every log record for the duration of an invocation. Where a
  log line must stay tied to a specific principal or session across lines, it
  carries a bounded, non-reversible token rather than the raw value: the backend
  uses `redact_identifier()` in `backend/src/utils/security.py` (a keyed
  HMAC-SHA-256 prefix over a random per-process key) and the frontend uses
  `redact()` in `ui/src/utils/logger.ts`. The HMAC key is random per process and
  is never read from the environment, so a token is stable only within a single
  process and does not correlate across processes, restarts, or tiers. The
  frontend also logs AgentCore's service request ID from the `x-amzn-RequestId`
  response header, which identifies the invocation to the service; it differs
  from the request ID the runtime container logs, so it does not join frontend
  and runtime log lines. That value is not derived from caller identity or
  content and is logged normalized rather than redacted.
- Every externally influenced log field is normalized before emission so
  carriage returns, line feeds, other C0/C1/DEL control characters, the Unicode
  line and paragraph separators, and bidirectional-formatting controls cannot
  create additional log records or reorder a rendered line. The backend
  normalizes every record once at the logging sink (the logger patcher applies
  `normalize_log_value()`); the frontend logger strips the same classes in
  `logInfo`, `logError`, `logWarning`, `logDebug`, and `redact`.
- The AgentCore SDK's own log handler would otherwise emit the raw runtime
  session ID on every invocation; `agentcore_main` wraps its formatter so the
  serialized `sessionId` is replaced with a bounded token while `requestId` is
  kept.
- Semantic-memory logging records only outcomes and counts, never extracted
  names, memory content, or raw actor identifiers.
- The request-handling error paths that can see prompt or identity values — the
  AgentCore entrypoint, the orchestrator (including the model-loop failure over
  the user prompt), the specialists, agent timing, and semantic memory — record
  the exception class name and a typed, sanitized error code (plus the request
  ID) through `log_sanitized_exception()`, never the exception message, since a
  provider or SDK error body can quote caller values. The full traceback is
  emitted only when debug logging is explicitly enabled (a non-production
  switch). Authentication errors are generic to clients and logs. Separately
  governed diagnostic paths that do not carry prompt or identity values retain
  the exception message to classify the failure: startup credential and
  container pre-warm checks and the memory-ID config read in `agentcore_main`,
  the GameLift specialist's AWS SDK describe/list calls, the Cost Explorer
  diagnostic path, the cost-report snapshot store's DynamoDB failure paths, and
  the Bedrock Prompt Management load fallback. Prepared operations and executor payloads must not contain
  tokens, email addresses, or display names.

A static regression test
(`backend/tests/unit/test_log_static_regression_unit.py`) scans every module in
`backend/src` so a future prompt/identity/session log is caught in review, and
includes self-tests over known bypass shapes so the guard cannot quietly stop
working.

## References

- [Amazon Cognito access-token claims](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html)
- [API Gateway JWT authorizer validation](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html)
