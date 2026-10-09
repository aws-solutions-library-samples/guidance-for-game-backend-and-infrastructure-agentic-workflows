# ADR 0007: Adopt Cloudscape With a Parallel-Shell Cutover

- **Status:** Accepted
- **Date:** 2026-10-08
- **Decision issue:** [#546](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/546)
- **Program:** [#266](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/266)

## Context

The web UI is a Next.js Pages Router application (Next 16, React 19) that
renders chat through `@copilotkit/react-ui` `CopilotChat`, pointed at the
`/api/copilot/chat` route. Charts are an owned SVG renderer with an accessible
data-table fallback. Theming uses `--ga-*` CSS tokens and a saved light or dark
preference. The program wants the UI to use the Cloudscape Design System so it
can host an operations experience with consistent, accessible AWS-style
components.

The constraint that shapes every decision below: users see no change until one
small, reversible cutover. Each change must leave the current UI's bundle and
rendering unchanged, and the new UI must build, hydrate without warnings, and
run under the production Content Security Policy.

A throwaway spike measured the chosen integration. Its versions, commands, and
numbers are in the Evidence section; the decisions reference it directly.

## Decisions

### 1. Delivery model — a second shell, server-selected

Build a second UI shell beside the current one. The frontend server chooses
which shell renders; the current UI is the default. A single page never mixes
the two design systems.

- **Chosen:** parallel shells with a server-side selector.
- **Alternatives rejected:** in-place restyling (users would see a
  half-migrated mix of two design systems during the migration); one large
  pull request (unreviewable, and impossible to ship without a user-visible
  change until the very end).
- **Consequences:** two shells coexist until cutover; some chat and chart glue
  is implemented twice during the migration; the current UI is provably
  untouched on every change (Evidence E2).

### 2. Shell selection — server default plus optional preview

The server resolves the shell from a deployment default and an optional
per-request preview signal. The default is a single configuration value, so
cutover and rollback need no rebuild. Administrators may opt a session into the
Cloudscape shell for preview before cutover.

- **Chosen:** an environment-backed default shell with an opt-in preview
  cookie, resolved server-side.
- **Alternatives rejected:** a build-time flag (cutover and rollback would
  require a rebuild and redeploy); a client-only toggle (the choice would not
  survive the first server render and could flash the wrong shell).
- **Consequences:** cutover (#552) is one config change; rollback is the same
  change reversed; a preview path exists for parity validation without exposing
  the new shell to all users.

### 3. Next.js Pages Router integration

- `@cloudscape-design/components` and `@cloudscape-design/chat-components` are
  added to `transpilePackages`; the published files use ESM and per-component
  entry points that Next must transpile (Evidence E1).
- Components are imported per path (for example
  `@cloudscape-design/components/app-layout`), never as a barrel, so unused
  components are tree-shaken.
- `@cloudscape-design/global-styles` is imported from the Cloudscape shell
  entry module only. It is never imported by the current UI or by any shared
  module, so the current UI loads no Cloudscape global CSS (Evidence E2).
- `AppLayout`, `TopNavigation`, and the chat components render on the server and
  hydrate with no warnings under the production build (Evidence E3).
- Code splitting keeps all Cloudscape code on the Cloudscape route only; the
  current UI's chunks contain no Cloudscape code (Evidence E2).

- **Alternatives rejected:** barrel imports (defeat tree-shaking and inflate the
  bundle); a global CSS import in `_app` or `_document` (would load Cloudscape
  styles for current-UI users, violating decision 1).
- **Consequences:** the dependency matrix and `transpilePackages` grow; the
  global-styles import must stay confined to the new shell.

### 4. Chat — Cloudscape components over headless CopilotKit state

Render the Cloudscape chat components (`ChatBubble`, `Avatar`, `LoadingBar`,
`SupportPromptGroup`, and `PromptInput`) over CopilotKit's **headless** client
state from `@copilotkit/react-core` (`useCopilotChat`), leaving
`/api/copilot/chat` and its payloads unchanged. `@copilotkit/react-ui` is not
used by the new shell.

- **Chosen:** Cloudscape presentation driven by `useCopilotChat` from
  `@copilotkit/react-core`. The spike proved these headless hooks drive the
  Cloudscape chat surface — append a user message, observe loading, render
  returned messages — with no `@copilotkit/react-ui` import (Evidence E4).
- **Alternatives rejected:** keeping `@copilotkit/react-ui` and theming it to
  look like Cloudscape (two design systems on one page, and the heavy
  react-ui markdown and math-rendering chain stays); replacing the CopilotKit
  wire protocol (a separate decision — the protocol stays pinned at 1.10.6).
- **Consequences:** the markdown renderer, chart-fence interception, code-block
  filename/copy/download behavior, the duplicate-submission guard, and the
  new-chat reset must be reimplemented against headless state in the new shell
  (tracked by #549). Dropping react-ui from the new shell also removes the
  largest contributor to its bundle (Evidence E2).

### 5. Charts — restyle the owned SVG renderer with Cloudscape tokens

Keep the owned chart contract (`ChartSpec`, versioned, fail-closed) and its
accessible data-table fallback. Render charts by restyling the owned SVG
renderer with Cloudscape design tokens.

- **Chosen:** owned SVG renderer, restyled with Cloudscape tokens.
- **Alternatives rejected:** Cloudscape's newer Highcharts-based chart
  components (Highcharts is commercially licensed; this MIT-0 sample must not
  require it of its users); Cloudscape's legacy chart components (would replace
  the owned, validated contract and its one-line reading plus table fallback,
  with no accessibility or security gain over the SVG renderer already in use).
- **Consequences:** no charting dependency is added in either shell; the chart
  contract and validation are unchanged across shells (tracked by #550); only
  colors, fonts, and spacing move to Cloudscape tokens.

### 6. Theming — bridge `--ga-*` tokens to Cloudscape modes

Map the `--ga-*` tokens to Cloudscape design tokens, keep each user's saved
light or dark preference (`game-agent-theme` in local storage), and switch
visual mode with `applyMode` from `@cloudscape-design/global-styles`.

- **Chosen:** a bridge layer that reads the existing saved preference and calls
  `applyMode(Mode.Light | Mode.Dark)`; `--ga-*` tokens are defined in terms of
  Cloudscape tokens in the new shell.
- **Alternatives rejected:** a second, independent theme store for the new
  shell (two sources of truth; the saved preference would not carry across a
  cutover); hard-coded colors (loses light/dark and the existing preference).
- **Consequences:** the saved preference carries across cutover and rollback
  unchanged; the theme toggle calls `applyMode` and persists the same key.

### 7. Content Security Policy

The current policy already allows what Cloudscape's stylesheet needs:
Cloudscape documents `default-src 'self'; style-src 'self'; font-src data:;
img-src blob:`, and the production policy already has `style-src 'self'
'unsafe-inline'`, `font-src 'self' data:`, and `img-src 'self' data: blob:`.
Under the current policy, both shells load with zero CSP violations
(Evidence E3). The global-styles package injects no inline `<style>` tags
(Evidence E3).

Decision: do **not** assume `style-src 'unsafe-inline'` can be dropped at
cutover. The spike measured that Cloudscape components set dynamic layout and
theming values through inline `style` attributes on their own elements, so
forcing `style-src 'self'` without `'unsafe-inline'` produced `style-src-attr`
violations against Cloudscape elements (Evidence E5). Dropping react-ui removes
one source of inline styling, but Cloudscape itself still relies on inline
style attributes. Tightening `style-src` is therefore deferred to #553 and
treated as an open risk (see Open Risks); it is only safe with a nonce or hash
strategy for style attributes, verified in a deployed environment.

- **Alternatives rejected:** asserting at this ADR that removing react-ui lets
  `style-src` drop `'unsafe-inline'` (the measurement contradicts it);
  loosening the policy further for Cloudscape (unnecessary — the stylesheet
  runs under `style-src 'self'`).
- **Consequences:** no CSP change is required to adopt Cloudscape; the policy
  stays as-is through cutover; a later, separately verified change may tighten
  style attributes.

### 8. Testing

- Jest: add `@cloudscape-design` to `transformIgnorePatterns` so babel-jest
  transpiles Cloudscape's ESM; without it, importing a Cloudscape component
  fails with a require-of-ESM error (Evidence E1). With that one change,
  Cloudscape's `test-utils/dom` `createWrapper` works under the existing jsdom
  environment and babel transform. Exclude the standalone build output from the
  Jest haste map (`modulePathIgnorePatterns`) so a prior production build does
  not cause a module-name collision.
- Playwright and automated accessibility checks run against both shells until
  cutover, using the preview selector from decision 2.

- **Alternatives rejected:** mocking Cloudscape in Jest (would not test real
  rendering or the test utilities); switching Jest to native ESM (a larger,
  unrelated change to the whole frontend test setup).
- **Consequences:** the Jest and Playwright configuration changes land with the
  foundation (#547); both shells are tested on every change until #553.

### 9. Operations surfaces

The operator console (#437) and the approval view (#554) are built **only** in
the new Cloudscape shell. They are hidden by default behind the existing
operator UI gate and appear only after capability discovery confirms
availability.

- **Chosen:** build operator surfaces once, in the new shell.
- **Alternatives rejected:** building them in the current UI and porting later
  (double the work on a UI that is being retired; a worse starting point for
  Cloudscape tables, status indicators, and confirmation dialogs).
- **Consequences:** #551 and #554 target the new shell; these surfaces are a
  reason the new shell must reach parity before cutover, but they stay hidden
  and change no backend or IAM behavior.

### 10. Cutover and rollback

Cutover (#552) flips the default shell with the single configuration value from
decision 2, after the parity checklist passes. Rollback is the same value
reversed, with no rebuild. The current UI shell is removed only after a soak
period.

- **Soak period:** keep the current UI shell deployable for **30 days** after
  the default switch. Removal is #553 and happens only if no rollback was
  needed during the soak.
- **Rollback:** revert the default shell value and redeploy configuration; no
  image rebuild. The saved theme preference, URLs, cookies, sessions, and the
  chat API are identical across shells, so rollback restores the current UI with
  no user-visible migration.

#### Parity checklist (for #552)

- [ ] Sign-in, token refresh, idle warning, automatic sign-out, cross-tab
      sign-out, and manual sign-out behave identically.
- [ ] Chat request and response contract with `/api/copilot/chat` is unchanged;
      the frontend API tests pass unmodified.
- [ ] Deterministic cost reports render verbatim: no total, ranking, or
      percentage differs between shells.
- [ ] Markdown and code blocks keep filename, copy, and download behavior.
- [ ] Charts render for line, grouped bar, and stacked area with the one-line
      reading and the data-table fallback.
- [ ] Light and dark mode honor the saved preference and switch with the toggle.
- [ ] Administrator and operator gating behave identically; hidden surfaces stay
      hidden.
- [ ] Keyboard, focus, screen-reader, and automated accessibility checks pass in
      light and dark mode.
- [ ] A refresh deployment to a maintainer test environment passes authenticated
      browser checks with the new default, and again after a rollback.

## Bundle-size budget

Measured as gzipped first-load JavaScript per route (Evidence E2). The budget
is a ceiling enforced in review when each shell lands:

| Route | Baseline (current UI) | Budget (ceiling) |
|---|---|---|
| Current UI `/` | ~823 kB | ~835 kB (no regression; must load no Cloudscape) |
| Current UI `/admin/users` | ~159 kB | ~165 kB (must load no Cloudscape) |
| New Cloudscape shell (chat) | n/a | ~500 kB |

Rules:

- The current UI's first-load JS must not regress and must contain **no**
  Cloudscape code or global CSS. The spike measured the current UI unchanged
  within build-identifier noise and carrying no Cloudscape chunk (Evidence E2).
- The new shell's chat route must stay at or below ~500 kB gzip first-load. The
  spike's minimal shell measured ~434 kB — lighter than the current UI because
  it drops `@copilotkit/react-ui`. The budget leaves headroom for the full chat
  feature set (markdown, charts, code blocks) while staying below the current
  UI's weight.
- Operator surfaces (#551, #554) must be in their own split chunks, not in the
  chat route's first load.

## Evidence (throwaway spike)

The spike was built outside the repository from `git archive HEAD ui`, extended
with the Cloudscape packages and a minimal server-selected second shell
(`AppLayout` + `TopNavigation` + a `ChatBubble`/`Avatar`/`LoadingBar` +
`PromptInput` conversation driven by `useCopilotChat`), with the current UI as
the default. It was deleted after measurement; it is not part of this change.

Exact versions: next 16.3.8, react 19.2.8, react-dom 19.2.8,
@copilotkit/react-core 1.10.6, @copilotkit/react-ui 1.10.6,
@cloudscape-design/components 3.0.1396,
@cloudscape-design/global-styles 1.0.71,
@cloudscape-design/chat-components 1.0.178, @playwright/test 1.63.0,
jest 30.5.1. Node 24.18.1.

**E1 — build and integration.** `next build` succeeded with the Cloudscape shell
present as a server-rendered route and the current pages still static.
Cloudscape's published files are ESM with per-component entry points, so Next
requires `@cloudscape-design/components` and `@cloudscape-design/chat-components`
in `transpilePackages`, and Jest requires `@cloudscape-design` in
`transformIgnorePatterns`.

**E2 — bundle size and isolation.** Gzipped first-load JS: current UI `/`
≈ 822.6 kB baseline versus ≈ 825.0 kB with the spike present (the delta is
build-identifier and manifest noise, not Cloudscape code); `/admin/users`
≈ 159.0 → 160.0 kB; shared ≈ 140.0 kB unchanged; new Cloudscape shell
≈ 433.5 kB. A content scan of every chunk reachable from `/` and
`/admin/users` found **no** Cloudscape code; only the Cloudscape route's chunk
contained it. This proves the current UI loads no Cloudscape JS or global CSS.

**E3 — production run under CSP, headless Chromium (Playwright).** With
`next start` serving the production build and headers, both the current `/` and
the Cloudscape shell loaded with the production CSP header applied. Captured per
shell: **0 CSP violations**, **0 React hydration warnings**, **0 page errors**.
The Cloudscape shell mounted its conversation and hydrated 43 Cloudscape
(`awsui_`) elements including `TopNavigation` and `PromptInput`, with **0**
injected inline `<style>` tags. (In the no-backend harness both shells logged
an environment-only error — a missing Cognito/`/api/config` backend — which
appears identically on the current UI and is unrelated to Cloudscape.)

**E4 — headless CopilotKit drives Cloudscape chat.** `useCopilotChat` from
`@copilotkit/react-core` drove the Cloudscape `ChatBubble`/`PromptInput`
conversation with no `@copilotkit/react-ui` import: appending a user message,
reading `isLoading`, and rendering `visibleMessages` all worked. The hook is
documented as working without a public license key.

**E5 — can `style-src` drop `'unsafe-inline'`?** No, not as-is. Serving the
Cloudscape shell under a forced `style-src 'self'` (no `'unsafe-inline'`)
produced `style-src-attr` violations against Cloudscape's own elements
(`AppLayout` main region, container, and the `PromptInput` textarea), because
Cloudscape sets dynamic layout and theming values via inline `style`
attributes. Removing `@copilotkit/react-ui` removes one source of inline
styling but not Cloudscape's. Cloudscape's documented `style-src 'self'` refers
to its external stylesheet (confirmed: no inline `<style>` tags), not to inline
style attributes.

**E6 — Jest with Cloudscape test utilities.** Importing a Cloudscape component
in Jest first failed with a require-of-ESM error. Adding `@cloudscape-design`
to `transformIgnorePatterns` (and excluding `.next/` from the haste map) made
two tests pass that render Cloudscape `Button` and `PromptInput` and locate
them with `createWrapper` from `@cloudscape-design/components/test-utils/dom`,
under the existing jsdom + babel-jest configuration.

Spike file names (for reference, not committed): `ui/src/lib/shellSelection.ts`,
`ui/src/pages/shell-cloudscape.tsx`, `ui/src/__tests__/spike-cloudscape.test.tsx`,
`ui/pw-check.mjs`, `ui/pw-csp-probe.mjs`, `ui/pw-strict-csp.mjs`,
`ui/pw-inline-src.mjs`, plus `transpilePackages`, `transformIgnorePatterns`,
and a dev-only skip-auth harness edit. Commands: `npm ci`;
`npm install --save-exact @cloudscape-design/components@3.0.1396
@cloudscape-design/global-styles@1.0.71
@cloudscape-design/chat-components@1.0.178`; `next build`; `next start -p 3999`;
Playwright scripts run with `node`; `npx jest`.

## Open Risks

- **Tightening `style-src` (decision 7, #553).** Dropping `'unsafe-inline'` from
  `style-src` is not safe without a nonce or hash strategy for inline style
  attributes used by Cloudscape. This must be designed and verified in a
  deployed environment before #553 claims the policy is tightened.
- **Harness auth.** The spike ran without a live Cognito backend, so the
  authenticated journeys (sign-in, refresh, idle warning) were not exercised
  end to end in the spike; #547 and #548 validate those against both shells.
- **Full chat parity bundle.** The ~434 kB new-shell measurement is a minimal
  conversation. The ~500 kB budget must be re-measured as markdown, charts, and
  code-block behavior are added (#549, #550).

## Consequences

- Adopting Cloudscape requires no change to `/api/copilot/chat`, the chart
  contract, the saved theme preference, URLs, sessions, or the production CSP.
- The current UI is provably unchanged on every migration change and can be
  restored by a single config reversal during a 30-day soak.
- The child issues (#547–#553) and the operator and approval work (#551, #554)
  inherit concrete, measured constraints from this record.

## Rejected Alternatives (summary)

- In-place restyling of the current UI.
- A single large migration pull request.
- Keeping `@copilotkit/react-ui` and theming it to resemble Cloudscape.
- Cloudscape's Highcharts-based chart components (commercial license).
- Replacing the owned chart contract with Cloudscape's legacy charts.
- Asserting the CSP can drop `style-src 'unsafe-inline'` at adoption.
