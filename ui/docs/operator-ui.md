# Operator UI (E4, issue #416)

The operator UI is a capability-gated console for the operations control plane.
It is UI-local: it owns only frontend files, tests, and docs, and it builds
against the **frozen E4 v1 contracts** in
`backend/src/operations/contracts/` — it never modifies backend or
infrastructure.

The entire surface is build-gated by
`NEXT_PUBLIC_OPERATIONS_UI_ENABLED=true`. The setting is exact and defaults to
false. When false, navigation makes no capability-discovery request, no operator
link appears, and server-side routing returns a 404 for `/operations`. Backend
authorization remains mandatory when the surface is enabled; this build gate
cannot grant authority.

## Surface

- **Navigation entry** — a single "Operations" link that appears only when the
  signed-in user is an admin **and** capability discovery reports a capability
  that is `available` and `provisioned`. Visibility is a UX affordance, never
  authorization.
- **`/operations` page** — composes:
  - a paginated, accessible **operation list** (loading / empty / error+retry
    states, keyboard-activatable rows);
  - an on-demand **detail timeline** distinguishing the proposal, authorization,
    approval, dispatch, execution, verification, rollback, and terminal facets,
    plus bounded provider-free evidence summaries. For a **pre-dispatch**
    operation (`prepared`, `pending_approval`, `approved`) it offers a
    confirmation-gated **Cancel operation** control; once the operation has
    dispatched or reached a terminal state the control disappears. Expiry is
    **never** offered as a human action — it is system-owned (see below);
  - a **kill-switch panel** with the deployment master switch and the single
    capability's prepare/dispatch/execute toggles, a static/dynamic **gate
    panel**, an explicit confirmation dialog (focus managed), a compare-and-set
    on `config_version`, an explicit retry against the authoritative durable
    version after a conflict, polite/assertive live regions, and loading/error/empty
    states.

## Server-side proxy routes

The browser never talks to the backend directly. Same-origin Next.js API routes
proxy the frozen routes and are the security boundary:

| UI route | Backend (frozen) route | Backend API |
| --- | --- | --- |
| `GET /api/operations/capabilities` | `GET /operations/capabilities` | E4 control plane |
| `GET /api/operations` | `GET /operations` | E4 control plane |
| `GET /api/operations/[operationId]` | `GET /operations/{operationId}` | E4 control plane |
| `GET /api/operations/kill-switch` | `GET /operations/control/kill-switch` | E4 control plane |
| `POST /api/operations/control` | `POST /operations/control` | E4 control plane |
| `POST /api/operations/[operationId]/cancel` | `POST /operations/{operationId}/cancel` | **E2 action API** |

Each route:

1. Verifies the **ID token** (admin group) and the **access token**, bound to
   the same subject.
2. Forwards **only** the verified access token as a `Bearer` credential — never
   the cookie, never the ID token. Tokens never reach browser JS.
3. Same-origin **CSRF** check on the state-changing routes (`control` and
   `cancel`).
4. Validates untrusted inputs against the frozen bounds before calling the
   backend (page size ≤ 50, known states, `op_` id pattern, opaque cursor) and
   rejects control bodies carrying identity/credential/policy.
5. Validates every upstream response with the TypeScript **schema guards** and
   **fails closed** (502) on any contract violation, never echoing the raw
   upstream body.

### Cancellation vs. expiry (lifecycle ownership)

Cancellation is owned by the **E2 approval/decision service**
(`backend/src/operations/decisions.py`), not the E4 control plane, so it is
proxied to a **separate server-only base URL** and kept in its own route map
(`OPERATIONS_ACTION_ROUTES`). A cancel is permitted only for the pre-dispatch,
non-terminal states `prepared`, `pending_approval`, `approved` — the UI mirrors
the backend `_CANCELLABLE_STATES` via `isCancellable()`.

**Expiry is system-owned.** The backend transitions a due operation to `expired`
from its own trusted clock (`{"actor_type": "system", ...}`), with no caller
credential. The UI therefore never exposes an "expire" action; `expired` is a
terminal state you can observe, never one an operator triggers.

### Transport hardening (defense in depth)

Below the schema guards, every upstream call (`src/operations/proxy.ts`):

- is issued with `redirect: 'manual'`. A `3xx` upstream is treated as a contract
  violation and mapped to a bounded **502** — the forwarded `Bearer` credential
  is never replayed to a redirect target the upstream chose;
- requires the server-only base URL to be **HTTPS outside the explicit local-dev
  bypass**, so the credential is never sent in the clear;
- reads the response body under a hard **byte ceiling** (both the declared
  `Content-Length` and the streamed bytes) *before* JSON parsing; an oversize or
  malformed body maps to a bounded **502**.

## Server-side base URLs

Both resolve server-side only and are never exposed to the browser. Outside the
local-dev bypass each must be **HTTPS**.

- `GBAW_OPERATIONS_API_BASE_URL` — the **E4 control-plane** API (reads +
  kill-switch control), falling back to `BACKEND_URL` then
  `http://localhost:8080`.
- `GBAW_OPERATIONS_ACTION_API_BASE_URL` — the **E2 operations action** API
  (approval-lifecycle decisions such as cancel). This has **no fallback**: it
  must be explicitly configured, and a missing, blank, or malformed value
  **fails closed** with a bounded **502** before any upstream request, so
  cancellation is never silently misrouted to the E4 control-plane base,
  `BACKEND_URL`, or a localhost default. Must be **HTTPS** outside the local-dev
  bypass.

## Never rendered

The projections are public-safe by contract, and the schema guards additionally
**fail closed** if any of these ever appears: email, display name, token, ARN,
account id, fleet id, or a raw provider payload. The cancel response is projected
to a minimal `{ operation_id, new_state }` confirmation so the internal
state-change ledger record (prepared-operation hash, actor, correlation ids) is
never exposed to the browser.

## Framework decision

The surface reuses the existing Next.js/React/CopilotKit stack and `--ga-*`
design tokens rather than adopting Cloudscape (#266 is P3/needs-scope). See
[operator-ui-framework-decision.md](operator-ui-framework-decision.md) for the
evidence and the reskin-friendly boundaries.

## Tests

- `src/__tests__/operations/*` — schema guards, proxy auth, and transport
  hardening (redirect, oversize/malformed body, HTTPS-required base).
- `src/__tests__/api/operations/*` — proxy route authorization, CSRF, validation,
  fail-closed behavior, and the E2 cancel route (auth/CSRF/state/forwarding).
- `src/__tests__/components/operations/*` and `src/__tests__/pages/operations.test.tsx`
  — component and page behavior, each with `jest-axe` accessibility assertions,
  including the cancel control (cancellable-only, no expire action).
- `tests/e2e-operator-workflow.spec.ts` — mocked Playwright workflow (no live
  backend/AWS), including the cancel-and-refresh flow.
- `tests/live-operator.spec.ts` (+ `playwright.live-operator.config.ts`) —
  authenticated live scaffold against a deployed stack; opt-in, skipped by
  default, and never writes unless `LIVE_OPERATOR_ALLOW_CONTROL=true`.
