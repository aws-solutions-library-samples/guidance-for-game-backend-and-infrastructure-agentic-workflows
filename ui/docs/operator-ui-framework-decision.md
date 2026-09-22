# Operator UI framework decision (issue #416, E4)

## Decision

The E4 operator UI is built on the **existing Next.js (pages router) + React +
CopilotKit stack and the app's existing `--ga-*` CSS design tokens**. It does
**not** adopt AWS Cloudscape (or any new UI component framework).

This is a deliberate, evidence-based decision, not an omission.

## Context

Issue #416 (E4) adds an operator surface: a capability-gated navigation entry, a
paginated operation list, an operation detail timeline, kill-switch controls,
and a per-capability gate panel. A separate issue, **#266**, proposes migrating
the frontend to AWS Cloudscape. At the time of this work #266 is **P3 /
needs-scope** — it is neither scheduled nor scoped, and it would change the
whole app's component substrate, not just the operator surface.

## Options considered

| Option | What it means | Verdict |
| --- | --- | --- |
| **A. Reuse existing Next.js/React + `--ga-*` tokens** | Build operator views as React components using the same tokens, `fetchWithTimeout`, and server-side API-proxy pattern the rest of the app already uses. | **Chosen** |
| B. Adopt Cloudscape now (per #266) | Add `@cloudscape-design/*`, restyle at least the operator surface, and introduce a second design system alongside `--ga-*`. | Rejected |
| C. Introduce a different new component library (MUI, Chakra, etc.) | Same class of cost as B with none of #266's alignment. | Rejected |

## Evidence for reusing the existing stack (Option A)

1. **#266 is P3 / needs-scope.** Adopting Cloudscape for one feature would
   pre-empt an unscoped, unscheduled, app-wide decision and create a mixed
   design system (Cloudscape islands inside a `--ga-*` app) that #266 would then
   have to untangle. Deferring is the reversible choice.
2. **The stack already covers every #416 need.** The app already ships:
   - a server-side API-proxy pattern that forwards the verified Cognito access
     token cookie to the backend and never exposes it to browser JS
     (`pages/api/copilot/chat.ts`, `pages/api/admin/approve.ts`);
   - admin-group gating from verified JWT claims (`pages/api/auth/user.ts`,
     `pages/api/admin/*`);
   - same-origin CSRF defense for state-changing routes (`utils/csrf.ts`);
   - a token design system (`--ga-*` in `src/styles/globals.css`) with
     light/dark theming;
   - Jest + Testing Library + Playwright already wired.
   No #416 requirement is blocked by the absence of Cloudscape.
3. **Accessibility does not require Cloudscape.** WCAG-conformant operator views
   are achievable with semantic HTML, ARIA live regions, and focus management,
   verified in CI with `jest-axe`. Accessibility is enforced by tests, not by a
   vendor component library.
4. **Cost and risk.** Adding Cloudscape pulls a large dependency tree, a second
   theming model, and bundle weight for a single surface, and would need its own
   security/licensing review — disproportionate to E4.

## Reskin-friendly boundaries (so a future #266 migration is cheap)

The operator UI is structured so that a later Cloudscape migration touches
presentation only:

- **Data/transport is isolated from presentation.** All wire access goes through
  server-side proxy routes under `pages/api/operations/*` and typed client
  helpers in `src/lib/operations/`. Components receive already-validated,
  already-typed data and never call `fetch` against the backend directly.
- **Schema guards are the contract boundary.** `src/lib/operations/schema.ts`
  validates every server response against the frozen E4 v1 shapes. Swapping the
  component library does not change this layer.
- **Presentation is token-driven, not hard-coded.** Operator components style
  exclusively via `--ga-*` custom properties. A reskin remaps tokens (or
  replaces the leaf components) without touching data flow, gating, or a11y
  semantics.
- **Components are small and leaf-oriented** (list, detail timeline, kill-switch
  panel, confirm dialog), so they can be replaced one at a time.

## Revisiting this decision

If #266 is scoped and scheduled, migrate the whole app — including this operator
surface — to Cloudscape in that effort. The boundaries above make the operator
surface a low-cost part of that migration. Until then, this surface stays on the
existing stack.
