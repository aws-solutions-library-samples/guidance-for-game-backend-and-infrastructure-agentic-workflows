# ADR 0007: Adopt Cloudscape With a Parallel-Shell Cutover

- **Status:** Proposed
- **Date:** 2026-10-08
- **Decision issue:** [#546](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/546)
- **Program:** [#266](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/266)

## Status Rationale

This record is **Proposed**. It becomes **Accepted** when its pull request is
approved; a maintainer adds the `Accepted:` date at merge, matching ADR 0005's
format. The decisions rest on a throwaway spike whose measurements are
summarized under Evidence; approval is the review of that committed evidence.
The fourth acceptance condition of #546 — child issues updated to match these
decisions — is tracked in the pull request and is not complete at authoring
time.

## Context

The web UI is a Next.js Pages Router application (Next 16, React 19) that
renders chat through `@copilotkit/react-ui` `CopilotChat`, pointed at the
`/api/copilot/chat` route. Charts are an owned SVG renderer with an accessible
data-table fallback. Theming uses `--ga-*` CSS tokens and a saved theme
preference with three states — `light`, `dark`, and `system` — where `system`
is the default and resolves from `prefers-color-scheme`. The program wants the
UI to use the Cloudscape Design System so it can host an operations experience
with consistent, accessible AWS-style components.

The constraint that shapes every decision below: users see no change until one
small, reversible cutover. Each change must leave the current UI's rendered
output and client bundle unchanged, and the new UI must build, render on the
server, and run under the production Content Security Policy.

The Pages Router imposes two structural facts the decisions must respect.
First, first-party global CSS and the shared application chrome live in
`ui/src/pages/_app.tsx`; every page renders inside it. On `main`, `_app`
imports `../styles/globals.css` (which `@import`s `command-center-layout.css`
and `chat-layout.css`) and `@copilotkit/react-ui/styles.css`, and it wraps every
page in the current UI's loading screen, `CognitoAuth` sign-in, and
`IdleWarningDialog`. A second shell under the same `_app` would inherit the
current design system unless `_app` is made shell-aware. Second, `/` is
statically prerendered today, so the shell selector must not turn it into a
server-rendered route.

A throwaway spike measured the chosen integration. Its versions, commands, and
numbers are in the Evidence section; the decisions reference it directly.

## Decisions

### 1. Delivery model — a second shell, server-selected, with an isolated host

Build a second UI shell beside the current one. The frontend server chooses
which shell renders `/`; the current UI is the default. A single page never
mixes the two design systems, which requires `_app` to become shell-aware
rather than a single shared host.

- **Chosen:** parallel shells with a server-side selector and a shell-aware
  `_app` that renders exactly one design system per request.
- **Alternatives rejected:** in-place restyling (users would see a
  half-migrated mix of two design systems during the migration); one large
  pull request (unreviewable, and impossible to ship without a user-visible
  change until the very end).
- **Consequences:** two shells coexist until cutover; some chat and chart glue
  is implemented twice during the migration; the `_app` refactor is a change to
  current-UI code, so it must be proven to leave the current UI's rendered
  output and computed styles unchanged (Evidence E2, E7).

#### How `_app` splits (new, required by decision 1)

The Pages Router allows first-party global CSS only in `_app`, so the current
UI's `globals.css` and `@copilotkit/react-ui/styles.css` imports stay in
`_app`. Isolation is achieved by scope, not by moving the imports:

1. **CSS scope.** The page-wide element rules in `globals.css` that would
   otherwise reach any page — `html, body` background and text color,
   `#__next { overflow: hidden }`, `body { overflow: hidden }`, and the
   `*:focus` / `*:focus-visible` rules — are nested under a root attribute,
   `[data-ga-shell="current"]`. The current shell sets that attribute; the
   Cloudscape shell does not, so those rules do not apply to it. The
   `@copilotkit/react-ui` stylesheet remains loaded by `_app` for both shells,
   but its rules are class-scoped to `.copilotKit*` elements the Cloudscape
   shell never renders, so they do not restyle it. #553 removes that import
   once nothing uses react-ui.
2. **Chrome scope.** The loading screen, `CognitoAuth`, and `IdleWarningDialog`
   presentation separate from the session logic. `_app` chooses the chrome per
   shell from a static `shell` property on the page component (or the rewrite
   target), so each shell renders its own sign-in, loading, and idle
   presentation (#548 adds the Cloudscape versions). The session coordinator,
   token refresh, and idle timer are reused unchanged.
3. **Equivalence check.** The split is proven by comparing the current UI's `/`
   before and after: the raw server HTML differs only in build-ID churn and the
   one added shell-attribute initialization line, and the computed styles of
   `body`, `#__next`, and focus are identical (Evidence E7). "Unchanged" is
   defined as identical computed styles and rendered structure, not a
   byte-identical HTML string.

### 2. Shell selection — same URL, request-time rewrite, server-only default

The server resolves the shell for `/` at request time from a deployment default
and an optional per-request preview signal. Selection is a Next middleware
rewrite, so `/` stays statically prerendered and its chunks are unchanged; the
URL the user sees never changes. The default comes from a **server-only**
environment value, not a `NEXT_PUBLIC_` one, so it is read per request and is
not inlined into client bundles at build time.

- **Chosen:** a Next middleware rewrite at `/` keyed on a server-only
  environment value (for example `GBAW_UI_SHELL`, `current` by default). When
  set to the Cloudscape value, middleware internally rewrites `/` to the
  Cloudscape shell route; the response is served at `/` with no redirect
  (Evidence E8).
- **Alternatives rejected:** `getServerSideProps` on `/` (turns the static page
  into server-rendered output, changing current-UI rendering and violating the
  Context rule that each change leaves the current UI unchanged); a redirect to
  a separate URL (changes the URL, which #266 forbids); a build-time flag
  (cutover and rollback would require a rebuild and redeploy); a
  `NEXT_PUBLIC_` selector (inlined per image at build time, so it could not be
  flipped by configuration); a client-only toggle (the choice would not survive
  the first server render, and would not keep each shell's chunks and CSS
  separate — only server selection does that).
- **Consequences:** cutover (#552) and rollback are a single configuration
  change to the running container with no image rebuild (see decision 10 and
  the cutover path below); `/` stays static; the preview path uses the same
  per-request mechanism.

#### Config-only cutover path (new, required by decisions 2 and 10)

The deployed selector value lives in the `PrimaryContainer.Environment` block
of `infrastructure/cloudformation/02-frontend-ecs-express.yaml`, fed by a new
stack parameter (for example `UiShell`, default `current`). Flipping the shell
is a parameter-only CloudFormation update that both `scripts/deploy.sh` and the
PowerShell deployment path must support without rebuilding or repushing the
frontend image — `deploy.sh` otherwise builds and pushes a new image on every
run (Step 6) before deploying the stack (Step 7). A parameter-only flip is an
ECS rolling deployment, not an instant switch: during the rollout, tasks with
the old and new values both serve requests, so the two shells are briefly
served side by side. #552 adds the parameter and the parameter-only update path
to both workflows.

### 3. Next.js Pages Router integration

- `@cloudscape-design/components` and `@cloudscape-design/chat-components` are
  added to `transpilePackages`; the published files use ESM and per-component
  entry points that Next must transpile (Evidence E1). Cloudscape's Next.js
  guidance also lists `@cloudscape-design/component-toolkit`; the spike built
  with it present, and #547 either keeps it in the list or records that the
  build passed without it and why.
- Components are imported per path (for example
  `@cloudscape-design/components/app-layout`), never as a barrel, so unused
  components are tree-shaken.
- `@cloudscape-design/global-styles` is imported from the Cloudscape shell
  entry module only. It is never imported by the current UI or by any shared
  module, so the current UI loads no Cloudscape global CSS (Evidence E2).
- Under the production build and `next start`, the Cloudscape shell's
  `AppLayout`, `TopNavigation`, and chat components are **server-rendered**:
  the raw HTML response at `/` contains Cloudscape (`awsui_`) markup counted
  directly from the response, not from the live DOM (Evidence E3). The current
  `_app` initializes auth to `'loading'` and server-renders only its own
  loading screen, so the Cloudscape shell route carries its own server output
  through the rewrite rather than inheriting the current UI's loading state.
- Code splitting keeps all Cloudscape code on the Cloudscape route only; the
  current UI's chunks contain no Cloudscape code (Evidence E2).

- **Alternatives rejected:** barrel imports (defeat tree-shaking and inflate the
  bundle); a global CSS import in `_document` (the Pages Router cannot import
  CSS in `_document`, so this is not possible — scope under decision 1 is the
  mechanism instead).
- **Consequences:** the dependency matrix and `transpilePackages` grow; the
  global-styles import must stay confined to the new shell.

### 4. Chat — Cloudscape components over CopilotKit client state

Render the Cloudscape chat components (`ChatBubble`, `Avatar`, `LoadingBar`,
`SupportPromptGroup`, and `PromptInput`) over CopilotKit's client state from
`@copilotkit/react-core` (`useCopilotChat`), leaving the `/api/copilot/chat`
request contract unchanged. `@copilotkit/react-ui` is not used by the new shell.

- **Chosen:** Cloudscape presentation driven by `useCopilotChat` from
  `@copilotkit/react-core`. The spike drove the Cloudscape chat surface from
  these hooks — append a user message, observe loading, render returned
  messages — with no `@copilotkit/react-ui` import (Evidence E4).
- **Alternatives rejected:** keeping `@copilotkit/react-ui` and theming it to
  look like Cloudscape (two design systems on one page, and the heavy react-ui
  markdown and math-rendering chain stays); adopting the non-deprecated
  headless hook `useCopilotChatHeadless_c` (its `messages`/`sendMessage` API
  requires a CopilotKit Cloud `publicApiKey` and a third-party account — the
  same kind of user-facing obligation this ADR rejects for Highcharts in
  decision 5); owning a minimal client for the existing request contract (a
  real option — the new shell would call `/api/copilot/chat` directly against
  the fields the proxy consumes, removing the dependency on CopilotKit return
  values entirely; not chosen as the primary path because it duplicates
  streaming and thread
  handling that the pinned react-core provides, but it is the fallback if
  the deprecated returns are removed); replacing the CopilotKit wire protocol
  (a separate decision — the protocol stays pinned at 1.10.6).
- **Consequences:** the new shell depends on `useCopilotChat` return values that
  are deprecated in `@copilotkit/react-core` 1.10.6 — `visibleMessages`
  ("use for compatibility only") and `appendMessage` ("will be removed in a
  future major version"). The supported replacements live only on
  `useCopilotChatHeadless_c`, which requires a CopilotKit Cloud key, so any
  CopilotKit major upgrade becomes a chat rewrite of the new shell (or the
  move to an owned minimal client above). The markdown renderer, chart-fence
  interception, code-block filename/copy/download behavior, the
  duplicate-submission guard, and the new-chat reset must be reimplemented
  against this client state in the new shell (tracked by #549). Dropping
  react-ui from the new shell also removes the largest contributor to its
  bundle (Evidence E2).

### 5. Charts — restyle the owned SVG renderer with Cloudscape tokens

Keep the owned chart contract (`ChartSpec`, versioned, fail-closed) and its
accessible data-table fallback. Render charts by restyling the owned SVG
renderer with Cloudscape design tokens.

- **Chosen:** owned SVG renderer, restyled with Cloudscape tokens.
- **Alternatives rejected:** Cloudscape's newer Highcharts-based chart
  components (Highcharts is commercially licensed; this MIT-0 sample must not
  require it of its users); Cloudscape's legacy chart components (would replace
  the owned, validated contract and its one-line reading plus table fallback,
  with no accessibility or security gain over the SVG renderer already in use,
  and Cloudscape documents the legacy charts as receiving only bug fixes
  through v3).
- **Consequences:** no charting dependency is added in either shell; the chart
  contract and validation are unchanged across shells (tracked by #550); only
  colors, fonts, and spacing move to Cloudscape tokens.

### 6. Theming — bridge the saved preference to Cloudscape modes

Keep the tri-state `game-agent-theme` preference (`light`, `dark`, `system`;
`system` default) and drive Cloudscape's visual mode from the already-resolved
theme, not from the raw stored value.

- **Chosen:** a bridge that reads `useTheme().resolvedTheme` (`light` or
  `dark`) and calls `applyMode(Mode.Light | Mode.Dark)` from
  `@cloudscape-design/global-styles`. `ThemeProvider` already resolves `system`
  from `prefers-color-scheme` and reacts to OS changes and cross-tab `storage`
  events, so the bridge is a `useTheme()` consumer and inherits all three. The
  `--ga-*` tokens are redefined in the new shell in terms of
  `@cloudscape-design/design-tokens` **JS** exports, read at runtime and written
  with `style.setProperty`, because the token package exposes hashed custom
  property names (for example
  `colorBackgroundContainerContent = "var(--color-background-container-content-mjfil9, #ffffff)"`
  in 3.0.114) that must not be hard-coded in static CSS. The Sass export route
  is not used because the repository has no `sass` toolchain.
- **Alternatives rejected:** reading the raw stored value and mapping only
  `light`/`dark` (has no mapping for the default `system` state and ignores OS
  and cross-tab changes); a second, independent theme store for the new shell
  (two sources of truth; the saved preference would not carry across a
  cutover); hard-coded colors (loses light/dark and the existing preference);
  hard-coding the hashed Cloudscape custom-property names in CSS (breaks on any
  token-package upgrade).
- **Consequences:** the saved preference carries across cutover and rollback
  unchanged; #547's package list gains `@cloudscape-design/design-tokens`;
  applying the mode must run before first paint the way `_document`'s existing
  pre-paint script does, so dark-mode users do not see a light frame; `system`
  mode and an OS theme change are on the parity checklist.

### 7. Content Security Policy

The production policy already carries `style-src 'self' 'unsafe-inline'`,
`font-src 'self' data:`, and `img-src 'self' data: blob:`, which is a superset
of what Cloudscape's external stylesheet documents (`style-src 'self'`). Under
the current policy, both shells load with zero CSP violations and the
global-styles package injects no inline `<style>` elements (Evidence E3).

Decision: do **not** change the CSP to adopt Cloudscape, and do **not** assume
`'unsafe-inline'` can be dropped from `style-src` at cutover. Tightening is
deferred to #553 and treated as an open risk.

The reason is specific. Cloudscape's components apply dynamic layout and
theming values through server-rendered `style="…"` **attributes** on their own
elements — the spike's Cloudscape shell HTML carried three such attributes
(AppLayout header/footer height, split-panel sizing, avatar size) and zero
`<style>` elements. Forcing `style-src 'self'` without `'unsafe-inline'`
produced exactly three "Applying inline style violates…" reports against those
attributes (Evidence E5). This governs the remedy:

- CSP does not block client-side writes to `element.style` (the CSSOM path
  React uses for `style` props), so a nonce or hash cannot be the fix — and
  client-only rendering does not remove these particular violations, because
  they come from server-rendered attributes, not CSSOM writes.
- `style-src-attr` accepts only `'unsafe-hashes'`, `'unsafe-inline'`, and
  `'report-sample'`; nonces apply to `<style>` and `<script>` elements, never
  to attributes, and hashes with `'unsafe-hashes'` cannot cover dynamic layout
  values.
- The workable options for #553 are: split the policy into
  `style-src-elem 'self' 'nonce-…'` (tightening injected `<style>` elements)
  plus `style-src-attr 'unsafe-inline'` (leaving the attributes permitted); or
  keep the shell content client-rendered and re-verify, accepting that the
  server-rendered attributes still require `style-src-attr 'unsafe-inline'`.

- **Alternatives rejected:** asserting at this ADR that removing react-ui lets
  `style-src` drop `'unsafe-inline'` (the measurement contradicts it); applying
  a nonce or hash to style attributes (not valid CSP for attributes); loosening
  the policy further for Cloudscape (unnecessary — the external stylesheet runs
  under `style-src 'self'`).
- **Consequences:** no CSP change is required to adopt Cloudscape; the policy
  stays as-is through cutover; #553 designs and verifies the `style-src-elem` /
  `style-src-attr` split in a deployed environment before claiming the policy is
  tightened.

### 8. Testing

- Jest: add `@cloudscape-design` to the negative-lookahead exception list of
  `transformIgnorePatterns` (so babel-jest transpiles Cloudscape's ESM instead
  of ignoring it); without it, importing a Cloudscape component fails with a
  require-of-ESM error (Evidence E1, E6). With that one change, Cloudscape's
  `test-utils/dom` `createWrapper` works under the existing jsdom environment
  and `next/babel` transform. Exclude the `.next/` standalone build output from
  the Jest haste map (`modulePathIgnorePatterns`) so a prior production build
  does not cause a module-name collision — a collision that `output:
  'standalone'` could already cause before this ADR.
- Playwright and automated accessibility checks run against both shells until
  cutover, through a shell-aware page-object layer (the existing specs and
  helpers under `ui/tests/`, including the live shakedown helper, hard-code
  `.copilotKit*` and `.ga-*` selectors, so running "against both shells" means
  per-shell page objects, not a configuration flag). #547 adds the per-shell
  page objects and a named accessibility tool: `@axe-core/playwright`
  (Mozilla Public License 2.0), which the repository does not yet depend on and
  which needs the usual dependency and license review.

- **Alternatives rejected:** Cloudscape's official `@cloudscape-design/jest-preset`
  (2.0.64), which Cloudscape's testing page says "you must use" (rejected
  because the repository's Jest config is inline JSON in `ui/package.json` with
  a custom `next/babel` transform, and the single `transformIgnorePatterns`
  allowlist entry fits that setup without replacing the whole preset); mocking
  Cloudscape in Jest (would not test real rendering or the test utilities);
  switching Jest to native ESM (a larger, unrelated change to the whole
  frontend test setup).
- **Consequences:** the Jest and Playwright configuration changes land with the
  foundation (#547), along with the accessibility dependency and the per-shell
  page objects; both shells are tested on every change until #553.

### 9. Operations surfaces

The operator console (#437) and the approval view (#554) are built **only** in
the new Cloudscape shell. They sit behind the default-false operator UI gate
introduced by #437 and appear only after capability discovery confirms
availability. Shell selection (decision 2) never enables an operator surface:
the two are independent switches, and selecting or previewing the Cloudscape
shell shows no operator or approval UI unless the operator gate is also on.

- **Chosen:** build operator surfaces once, in the new shell, behind the #437
  gate.
- **Alternatives rejected:** building them in the current UI and porting later
  (double the work on a UI that is being retired; a worse starting point for
  Cloudscape tables, status indicators, and confirmation dialogs).
- **Consequences:** building #437 only in the new shell makes it depend on the
  Cloudscape foundation (#547/#548),
  which changes #437's current scope note that it uses "the existing Next.js
  design system" and is "independent of the separate Cloudscape redesign
  request"; through #437 this also sequences #441, #534, and #539. Because the
  operator and approval surfaces exist only in the new shell, a shell rollback
  during the soak would otherwise hide the approval view and remove the only
  non-API approval path while operations may be pending; therefore the preview
  path (decision 2) stays available to approvers after a rollback so #554 stays
  reachable. Whether #441 activation waits for cutover (#552) or relies on the
  preview path is #441's decision to record. These surfaces change no backend
  or IAM behavior (ADR 0001; a hidden UI control is never authorization).

### 10. Cutover and rollback

Cutover (#552) flips the default shell with the single configuration value from
decision 2, after the parity checklist passes. Rollback is the same value
reversed, applied as a parameter-only update (an ECS rolling deployment), with
no image rebuild. The current UI shell is removed only after a soak period.

- **Chosen:** flip the default, soak, then remove the old shell in a later
  change.
- **Alternatives rejected:** removing the current shell at cutover (no reversible
  path if a regression appears); soaking a single tagged release rather than a
  time-bounded window (a sample has no customer telemetry to judge a single
  release); keeping both shells indefinitely (two design systems and the
  selector maintained forever); treating rollback as reverting the frontend
  image (slower and coarser than reversing one configuration value, and it would
  also revert unrelated frontend fixes).
- **Soak period (defined in repository terms, since a public sample has no
  customer telemetry):** #553 — which removes the current shell and the
  selector — merges no sooner than **30 days** after #552 merges, and only when
  there is no open regression issue filed against the new shell and a maintainer
  test-environment run passes authenticated browser checks with the new default.
  "Keep the current UI deployable for 30 days" means the repository retains the
  current shell, its chunks, and the selector for that window.
- **Rollback:** reverse the default shell value and redeploy configuration; no
  image rebuild. The saved theme preference, URLs, cookies, sessions, and the
  chat request contract are identical across shells, so rollback restores the
  current UI with no user-visible migration. The preview path stays available to
  approvers after a rollback (decision 9).
- **Consequences:** two shells are maintained for at least 30 days after #552;
  the selector and the preview cookie live until #553; the config-only path in
  both deployment workflows (decision 2) is a prerequisite for #552.

#### Parity checklist (for #552)

- [ ] Sign-in, token refresh, idle warning, automatic sign-out, cross-tab
      sign-out, and manual sign-out behave identically.
- [ ] The `/api/copilot/chat` request contract is unchanged — the fields the
      proxy consumes (the last user message and the `threadId`); the frontend
      API tests pass unmodified.
- [ ] New-chat reset rotates the thread: it calls `reset()` **and** rotates the
      `threadId` through `useCopilotContext().setThreadId`, which is what clears
      the AgentCore session; `reset()` alone does not.
- [ ] Deterministic cost reports render verbatim: no total, ranking, or
      percentage differs between shells.
- [ ] Markdown and code blocks keep filename, copy, and download behavior.
- [ ] Charts render for line, grouped bar, and stacked area with the one-line
      reading and the data-table fallback.
- [ ] Theme parity: `light`, `dark`, and `system` honor the saved preference;
      an OS theme change updates both shells; the toggle switches modes with no
      wrong-mode flash on first paint.
- [ ] Operator and approval surfaces stay behind the #437 gate in the new shell;
      shell selection and preview never reveal them; after #473 the current UI
      has no admin page, so there is no current-shell operator surface to
      compare against.
- [ ] Keyboard, focus, screen-reader, and automated accessibility checks pass in
      light and dark mode in the new shell.
- [ ] A refresh deployment to a maintainer test environment passes authenticated
      browser checks with the new default, and again after a rollback.

## Bundle-size budget

Measured as summed gzip (zlib level 6) sizes, in KiB (1,024 bytes), of the
`.js` files listed under `pages[route] ∪ pages['/_app']` in
`.next/build-manifest.json` (Evidence E2, E10). Next 16 `next build` prints no
per-route sizes, so #547 commits this measurement as a script (under
`ui/scripts/`) and a `scripts/test` check, or records the numbers under
`docs/evidence/` in the ADR-0005 style; the budget is a ceiling checked that
way, not by eye.

The budget keys on `/_app` shared first-load plus **every current-UI page**,
rather than on a fixed route list, because #473 removes the `/admin/users` page
before the Cloudscape work lands.

| Scope | Baseline (current UI) | Budget (ceiling) |
|---|---|---|
| `/_app` shared first-load | 140.0 KiB | 145.0 KiB (no Cloudscape code) |
| Every current-UI page (today `/`) | `/` 822.6 KiB | +0 regression beyond build-ID churn; no Cloudscape code or global CSS |
| New Cloudscape shell (chat) | n/a | 500 KiB, **provisional** (see below) |

Rules:

- The current UI's first-load JS must not regress beyond build-ID churn and must
  contain **no** Cloudscape code or global CSS. The spike measured `/` moving
  from 822.6 to 823.7 KiB with the second shell present; the content scan found
  no Cloudscape code in `/`'s chunks, so the delta is Turbopack regrouping
  shared modules, not Cloudscape (Evidence E2, E10). The budget forbids any
  Cloudscape module and treats the small churn as noise, rather than claiming
  the chunks are byte-identical.
- The new-shell ceiling of 500 KiB is **provisional** until #549 measures the
  full chat feature set. The spike's minimal client-rendered shell measured
  323.3 KiB, but parity reuses the owned `ChatCodeBlock`, which imports the
  **full** `Prism` build from `react-syntax-highlighter` 16.1.1 (refractor
  5.0.0, 297 languages). Measured in isolation, that import alone is 226.6 KiB
  gzip, against the ~177 KiB of headroom under the ceiling — so parity would
  exceed 500 KiB (Evidence E9). The new shell therefore must use `PrismLight`
  with an explicit language allowlist (the same five-language allowlist measured
  24.5 KiB gzip) or lazy-load the highlighter; #549 confirms the ceiling once
  the real feature set is in place.
- A CSS budget applies too, because `globals.css` embeds fonts and Cloudscape's
  global stylesheet ships Open Sans; #547 records the gzipped CSS first-load per
  shell alongside the JS.
- Operator surfaces (#551, #554) must be in their own split chunks, not in the
  chat route's first load.

## Evidence (throwaway spike)

The spike was built outside the repository from `git archive HEAD ui`, extended
with the Cloudscape packages and a minimal server-selected second shell
(`AppLayout` + `TopNavigation` + a `ChatBubble`/`Avatar` + `PromptInput`
conversation driven by `useCopilotChat`), with the current UI as the default.
It was deleted after measurement; it is not part of this change. The build used
Turbopack, the Next 16 default (Cloudscape's bundling notes target webpack).

Exact versions: next 16.3.8, react 19.2.8, react-dom 19.2.8,
@copilotkit/react-core 1.10.6, @copilotkit/react-ui 1.10.6,
@cloudscape-design/components 3.0.1396,
@cloudscape-design/global-styles 1.0.71,
@cloudscape-design/chat-components 1.0.178,
@cloudscape-design/design-tokens 3.0.114,
@cloudscape-design/component-toolkit 1.0.0-beta.189, @playwright/test 1.63.0,
jest 30.5.1. Node 24.18.1.

**E1 — build and integration.** `next build` succeeded with the Cloudscape shell
present and the current pages still static (`/` and `/admin/users` remained
`○`). Cloudscape's published files are ESM with per-component entry points, so
Next requires `@cloudscape-design/components` and
`@cloudscape-design/chat-components` in `transpilePackages`, and Jest requires
`@cloudscape-design` in the `transformIgnorePatterns` exception list. The build
also had `@cloudscape-design/component-toolkit` in `transpilePackages`.

**E2 — bundle size and isolation.** Summed gzip-6 first-load JS from
`build-manifest.json`: current UI `/` 822.6 KiB baseline versus 823.7 KiB with
the spike present; `/admin/users` 159.0 → 160.1 KiB; `/_app` shared 140.0 →
140.1 KiB; new Cloudscape shell 323.3 KiB. A content scan of every `.js` chunk
reachable from `/` and `/admin/users` found **no** `awsui`/`cloudscape-design`
code (0 of 11 chunks for `/`, 0 of 10 for `/admin/users`); only the Cloudscape
route's chunk contained it (1 of 10). This shows the current UI loads no
Cloudscape JS and no Cloudscape global CSS.

**E3 — production server rendering under CSP (`next start`, headless Chromium).**
With `next start` serving the production build and the production CSP header,
`/` served the current UI (0 `awsui_` in the raw HTML response) by default, and
the Cloudscape shell served under the selector returned **71 `awsui_` substrings
in the raw HTML response** (counted from the server response body, not the live
DOM), with **0** server-rendered `<style>` elements. Both shells loaded under
the production CSP header with 0 violations. (In the no-backend harness both
shells logged one environment-only error — a missing Cognito/`/api/config`
backend — which appears identically on the current UI and is unrelated to
Cloudscape.)

**E4 — CopilotKit client state drives Cloudscape chat.** `useCopilotChat` from
`@copilotkit/react-core` drove the Cloudscape `ChatBubble`/`PromptInput`
conversation with no `@copilotkit/react-ui` import: appending a user message,
reading `isLoading`, and rendering `visibleMessages` all worked without a
public license key. In 1.10.6 the `visibleMessages` and `appendMessage` return
values this uses are marked deprecated; the supported replacements are on
`useCopilotChatHeadless_c`, which requires a CopilotKit Cloud key (decision 4).

**E5 — can `style-src` drop `'unsafe-inline'`?** No. The Cloudscape shell's raw
server HTML carried exactly **3 `style="…"` attributes** (AppLayout header and
footer height, split-panel sizing, avatar size — all CSS custom properties) and
**0 `<style>` elements**. Loading that HTML under a forced
`style-src 'self'` (no `'unsafe-inline'`) produced **3** "Applying inline style
violates…" reports, one per attribute. The violations are therefore
`style-src-attr` violations from server-rendered attributes, not from `<style>`
elements or client CSSOM writes. Nonces and hashes cannot apply to attributes
(decision 7).

**E6 — Jest with Cloudscape test utilities.** Importing a Cloudscape component
in Jest first failed with a require-of-ESM error. Adding `@cloudscape-design`
to the `transformIgnorePatterns` exception list (and excluding `.next/` from the
haste map) made a test pass that renders a Cloudscape `Button`/`PromptInput` and
locates it with `createWrapper` from
`@cloudscape-design/components/test-utils/dom`, under the existing jsdom +
babel-jest configuration. Cloudscape's testing page recommends its official
`@cloudscape-design/jest-preset` (2.0.64); the allowlist entry was chosen
instead (decision 8).

**E7 — current-UI equivalence across the `_app` split.** With the page-wide
`globals.css` rules scoped under `[data-ga-shell="current"]` and `_app` made
shell-aware, the current UI's `/` computed styles were identical before and
after the split: `body { overflow: hidden }`, body background
`rgb(246, 248, 252)`, `#__next { overflow: hidden }`, Inter font, theme mode
`system`. The raw server HTML differed only in build-ID/chunk-hash churn and the
single added shell-attribute initialization line; the server-rendered loading
screen was otherwise identical. "Unchanged" is defined as identical computed
styles and rendered structure.

**E8 — same-URL selection flips with no rebuild.** From one build, serving with
the server-only `GBAW_UI_SHELL` unset returned the current UI at `/` (0
`awsui_`); serving the **same** build with `GBAW_UI_SHELL=cloudscape` returned
the Cloudscape shell at `/` (71 `awsui_`) via a Next middleware rewrite —
`http_code=200`, `num_redirects=0`, URL still `/`. `/` remained statically
prerendered in both the build output and the served responses. No rebuild
occurred between the two cases.

**E9 — syntax-highlighter weight.** The owned `ChatCodeBlock` imports the full
`Prism` build (`import { Prism } from 'react-syntax-highlighter'`), which pulls
refractor 5.0.0's 297 language modules. Bundled and minified in isolation, that
import is 656 KiB raw / **226.6 KiB gzip**. `PrismLight` with a five-language
allowlist (typescript, python, bash, json, yaml) is 72 KiB raw / **24.5 KiB
gzip**. The 500 KiB new-shell ceiling has roughly 177 KiB of headroom over the
323.3 KiB minimal shell, so parity needs `PrismLight` or lazy loading
(decision 9's budget).

**E10 — measurement method.** Baseline numbers reproduce exactly from a clean
`git archive HEAD ui`, `npm ci`, `next build`: `/_app` 140.0, `/` 822.6,
`/admin/users` 159.0 KiB, as summed gzip-6 sizes of `pages[route] ∪
pages['/_app']` `.js` files divided by 1,024. The build ID appears only in
`_buildManifest.js`, `_ssgManifest.js`, and `_clientMiddlewareManifest.js`,
which sit in `lowPriorityFiles` outside the measured set, so a first-load delta
is changed chunk content, not build-ID noise.

**E11 — token package shape.** `@cloudscape-design/design-tokens` 3.0.114
exports hashed custom-property names, for example
`colorBackgroundContainerContent = "var(--color-background-container-content-mjfil9, #ffffff)"`
and `colorTextBodyDefault = "var(--color-text-body-default-b2us04, #0f141a)"`.
The names must be read from the package at runtime, not hard-coded in CSS
(decision 6).

Spike files (reference only, never committed; built from `git archive HEAD ui`,
then deleted): new `ui/src/middleware.ts`, `ui/src/pages/shell-cloudscape.tsx`,
`ui/src/lib/cloudscapeThemeBridge.ts`, and measurement scripts
`ui/pw-computed.mjs`, `ui/pw-isolation.mjs`, `ui/pw-strictcsp.mjs`,
`ui/prism-measure/full.mjs`, `ui/prism-measure/light.mjs`; edits to
`ui/src/pages/_app.tsx` (shell-aware split + `data-ga-shell`),
`ui/src/pages/_document.tsx` (default `data-ga-shell="current"`),
`ui/src/styles/globals.css` (page-wide rules scoped under
`[data-ga-shell="current"]`), and `ui/next.config.mjs` (`transpilePackages`).
Commands: `git archive HEAD ui | tar -x`; `npm ci`;
`npm install --save-exact @cloudscape-design/components@3.0.1396
@cloudscape-design/global-styles@1.0.71
@cloudscape-design/chat-components@1.0.178
@cloudscape-design/design-tokens@3.0.114 @cloudscape-design/component-toolkit`;
`next build`; `next start`; Playwright scripts run with `node`; `npx jest`;
`npx esbuild … --bundle --minify` plus `gzip -6` for E9.

## Open Risks

- **Tightening `style-src` (decision 7, #553).** Cloudscape sets dynamic values
  through server-rendered `style` attributes, which only `style-src-attr`
  governs and which nonces and hashes cannot cover. #553 must design and verify
  the `style-src-elem 'self' 'nonce-…'` / `style-src-attr 'unsafe-inline'` split
  (or an equivalent) in a deployed environment before claiming the policy is
  tightened.
- **Harness auth.** The spike ran without a live Cognito backend, so the
  authenticated journeys (sign-in, refresh, idle warning) were not exercised
  end to end; #547 and #548 validate those against both shells.
- **Full chat parity bundle.** The 323.3 KiB new-shell measurement is a minimal
  client-rendered conversation. The 500 KiB ceiling is provisional and must be
  re-measured as markdown, charts, and code-block behavior are added, with
  `PrismLight` or lazy loading for the highlighter (#549, #550).
- **First-paint shell attribute.** In the spike the `data-ga-shell` attribute
  was set by the pre-paint script on the client. For true first-paint CSS
  isolation the attribute should be written into the server HTML for the chosen
  shell; #547 confirms the server-side placement.

## Consequences

- Adopting Cloudscape requires no change to the `/api/copilot/chat` request
  contract, the chart contract, the saved theme preference, URLs, sessions, or
  the production CSP.
- The `_app` split is a change to current-UI code, proven to leave the current
  UI's computed styles and rendered structure unchanged (Evidence E7); the
  current UI can be restored by a single configuration reversal during a 30-day
  soak.
- The child issues (#547–#553) and the operator and approval work (#437, #551,
  #554) inherit concrete, measured constraints from this record.

## Rejected Alternatives (summary)

- In-place restyling of the current UI.
- A single large migration pull request.
- `getServerSideProps` on `/` or a redirect for shell selection (would change
  `/`'s rendering or its URL); a `NEXT_PUBLIC_` or build-time selector.
- Keeping `@copilotkit/react-ui` and theming it to resemble Cloudscape; the
  licensed `useCopilotChatHeadless_c` hook.
- Cloudscape's Highcharts-based chart components (commercial license); replacing
  the owned chart contract with Cloudscape's legacy charts.
- Reading the raw theme value instead of `resolvedTheme`; a second theme store;
  hard-coded colors; hard-coded hashed Cloudscape token names.
- Asserting the CSP can drop `style-src 'unsafe-inline'` at adoption; applying a
  nonce or hash to style attributes.
- Cloudscape's official Jest preset in place of the allowlist entry; mocking
  Cloudscape; native-ESM Jest.
- Removing the current shell at cutover; soaking a single tagged release;
  keeping both shells indefinitely; rolling back by reverting the frontend image.
