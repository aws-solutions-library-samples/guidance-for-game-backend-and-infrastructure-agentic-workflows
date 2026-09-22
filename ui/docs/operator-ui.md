# Operator UI (E4, issue #416)

The operator UI is a capability-gated console for the operations control plane.
It is UI-local: it owns only frontend files, tests, and docs, and it builds
against the **frozen E4 v1 contracts** in
`backend/src/operations/contracts/` — it never modifies backend or
infrastructure.

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
    plus bounded provider-free evidence summaries;
  - a **kill-switch panel** with the deployment master switch and the single
    capability's prepare/dispatch/execute toggles, a static/dynamic **gate
    panel**, an explicit confirmation dialog (focus managed), a compare-and-set
    on `config_version`, polite/assertive live regions, and loading/error/empty
    states.

## Server-side proxy routes

The browser never talks to the backend directly. Same-origin Next.js API routes
proxy the frozen E4 routes and are the security boundary:

| UI route | Backend (frozen) route |
| --- | --- |
| `GET /api/operations/capabilities` | `GET /operations/capabilities` |
| `GET /api/operations` | `GET /operations` |
| `GET /api/operations/[operationId]` | `GET /operations/{operationId}` |
| `GET /api/operations/kill-switch` | `GET /operations/control/kill-switch` |
| `POST /api/operations/control` | `POST /operations/control` |

Each route:

1. Verifies the **ID token** (admin group) and the **access token**, bound to
   the same subject.
2. Forwards **only** the verified access token as a `Bearer` credential — never
   the cookie, never the ID token. Tokens never reach browser JS.
3. Same-origin **CSRF** check on the state-changing control route.
4. Validates untrusted inputs against the frozen bounds before calling the
   backend (page size ≤ 50, known states, `op_` id pattern, opaque cursor) and
   rejects control bodies carrying identity/credential/policy.
5. Validates every upstream response with the TypeScript **schema guards** and
   **fails closed** (502) on any contract violation, never echoing the raw
   upstream body.

The backend base URL comes from `GBAW_OPERATIONS_API_BASE_URL` (server-side
only), falling back to `BACKEND_URL` then `http://localhost:8080`.

## Never rendered

The projections are public-safe by contract, and the schema guards additionally
**fail closed** if any of these ever appears: email, display name, token, ARN,
account id, fleet id, or a raw provider payload.

## Framework decision

The surface reuses the existing Next.js/React/CopilotKit stack and `--ga-*`
design tokens rather than adopting Cloudscape (#266 is P3/needs-scope). See
[operator-ui-framework-decision.md](operator-ui-framework-decision.md) for the
evidence and the reskin-friendly boundaries.

## Tests

- `src/__tests__/operations/*` — schema guards and proxy auth.
- `src/__tests__/api/operations/*` — proxy route authorization, CSRF, validation,
  and fail-closed behavior.
- `src/__tests__/components/operations/*` and `src/__tests__/pages/operations.test.tsx`
  — component and page behavior, each with `jest-axe` accessibility assertions.
- `tests/e2e-operator-workflow.spec.ts` — mocked Playwright workflow (no live
  backend/AWS).
- `tests/live-operator.spec.ts` (+ `playwright.live-operator.config.ts`) —
  authenticated live scaffold against a deployed stack; opt-in, skipped by
  default, and never writes unless `LIVE_OPERATOR_ALLOW_CONTROL=true`.
