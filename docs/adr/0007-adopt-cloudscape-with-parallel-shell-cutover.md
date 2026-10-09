# ADR 0007: Adopt Cloudscape With a Parallel-Shell Cutover

- **Status:** Proposed
- **Date:** 2026-10-08
- **Decision issue:** [#546](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/546)
- **Program:** [#266](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/266)

## Status Rationale

This record is **Proposed**. It follows ADR 0005's two-step acceptance path:
this record merges as **Proposed**, and a later small pull request flips it to
**Accepted**. That follow-up changes the `Status:` line to `Accepted`, adds an
`Accepted:` date line (matching ADR 0005's format), rewrites this paragraph to
record the acceptance rationale, and updates the status cell in the ADR index
(`docs/adr/README.md`). The decisions rest on a throwaway spike whose
measurements are summarized under Evidence; the spike itself is not committed,
so the acceptance review is a review of those summarized measurements, not of
committed spike code. The fourth acceptance condition of #546 — child issues
updated to match these decisions — is applied by a maintainer and is not
complete at authoring time, so the status stays Proposed until both the child
issues and the status-flip pull request land.

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

1. **CSS scope.** Every element-level and universal selector in `globals.css`
   that would otherwise reach any page is nested under the `:where()` form of a
   root attribute, `:where([data-ga-shell="current"])`. The complete set is the
   `html, body` block (padding, margin, `font-family`, `line-height`, font
   smoothing, `background`, and `color`), the `body` text-rendering and
   `overflow` rules, `a { color: inherit; text-decoration: none }`,
   `* { box-sizing: border-box }`, `button { border: none; background: none;
   cursor: pointer; font-family: inherit }`, `#__next { height; overflow }`,
   `::selection`, and `*:focus` / `*:focus-visible`. `:where()` is used rather
   than a bare descendant selector so scoping adds **zero** specificity: a
   bare `[data-ga-shell="current"] *:focus` would raise the selector from
   (0,1,0) to (0,2,0) and `html`/`body` from (0,0,1) to (0,1,1), which could
   invert which rule wins against a competing element-level rule; `:where()`
   has specificity (0,0,0), so each scoped rule keeps the specificity it had on
   `main`. The current shell sets `data-ga-shell="current"`; the Cloudscape
   shell sets `data-ga-shell="cloudscape"`, so those rules never match it, and
   plain Cloudscape markup such as markdown links keeps Cloudscape's own
   normalize.css reset rather than inheriting the current UI's link and button
   resets. The one deliberate exception is the `prefers-reduced-motion`
   `*, *::before, *::after` block: it is an accessibility safeguard that must
   apply to both shells, so it stays unscoped. The `@copilotkit/react-ui`
   stylesheet remains loaded by `_app` for both shells, but its rules are
   class-scoped to `.copilotKit*` elements the Cloudscape shell never renders,
   so they do not restyle it. #553 removes that import once nothing uses
   react-ui.
2. **Chrome scope.** The loading screen, `CognitoAuth`, and `IdleWarningDialog`
   presentation separate from the session logic. `_app` chooses the chrome per
   shell from a static `shell` property on the page component (or the rewrite
   target), so each shell renders its own sign-in, loading, and idle
   presentation (#548 adds the Cloudscape versions). `_app` only **selects** the
   chrome; it never imports Cloudscape chrome. The Cloudscape sign-in, loading,
   and idle components come from the Cloudscape page module or through
   `next/dynamic`, so no Cloudscape JS or CSS enters `_app`'s shared first-load
   (the bundle budget forbids Cloudscape code in `/_app` and in every
   current-UI page). The session coordinator, token refresh, and idle timer are
   reused unchanged.
3. **Equivalence check.** The split is proven by comparing the current UI's `/`
   before and after: the raw server HTML differs only in build-ID churn and one
   added `<html>` attribute (`data-ga-shell`), and the computed styles of
   `body`, `#__next`, and `:focus` / `:focus-visible` are identical
   (Evidence E7). "Unchanged" is defined as identical computed styles and
   rendered structure, not a byte-identical HTML string.

`data-ga-shell` is written into the server HTML per prerendered page, so the
correct shell's rules apply at first paint with no client step. A class
`_document` reads `this.props.__NEXT_DATA__.page` and maps it to the shell with
`SHELL_BY_PAGE[page] ?? 'current'`, setting the attribute on `<Html>`. In the
spike this emitted `data-ga-shell="current"` for `/` and
`data-ga-shell="cloudscape"` for the rewritten Cloudscape page, and both pages
stayed statically prerendered (`○`), so writing the attribute at build time does
not make `/` dynamic. The Pages Router hydrates into `#__next`, not `<html>`, so
a server-written `<html>` attribute causes no hydration mismatch.

### 2. Shell selection — same URL, request-time rewrite, server-only default

The server resolves the shell for `/` at request time from a deployment default
and an optional per-request preview signal. Selection is a Next proxy
(`proxy.ts`) rewrite, so `/` stays statically prerendered and its chunks are
unchanged; the URL the user sees never changes. Next 16 deprecates the
`middleware` file convention in favor of `proxy`, which runs only on the
Node.js runtime; the build prints a deprecation warning for `middleware.ts`, so
the record names `proxy.ts`. The spike reproduced the same flip under both
conventions. The default comes from a **server-only** environment value, not a
`NEXT_PUBLIC_` one, so it is read per request and is not inlined into client
bundles at build time.

- **Chosen:** a Next proxy (`proxy.ts`) rewrite at `/` keyed on a server-only
  environment value (for example `GBAW_UI_SHELL`, `current` by default). When
  set to the Cloudscape value, the proxy internally rewrites `/` to the
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
  per-request mechanism. The rewrite leaves the internal Cloudscape route (for
  example `/shell-cloudscape`) directly addressable, and the rewrite response
  carries an `x-middleware-rewrite` header naming it. This grants nothing: the
  preview is a presentation choice open to anyone, selecting only which design
  system renders. It never enables an operator or approval surface (decision 9),
  which stays behind the #437 gate regardless of shell. If #547 prefers to hide
  the internal route, the proxy can return 404 for direct requests to it; either
  way the preview confers no authority.

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
  directly from the response, not from the live DOM (Evidence E3). Static
  prerendered HTML cannot know auth state, so this holds only under a specific
  harness choice: the Cloudscape branch renders the page before auth settles
  and gates on the client, rather than server-rendering only a loading screen
  the way the current `_app` does (which initializes auth to `'loading'`). That
  ungated-render-then-client-gate design is what #548 builds, and it is what the
  spike measured to count the Cloudscape markup; which part of the Cloudscape
  shell appears in the server HTML before auth settles therefore determines
  E5's attribute count. #546's acceptance also asks that the shell render on the
  server without hydration warnings: the E3 run reported no hydration errors
  for the Cloudscape shell. If a later harness change reintroduces a mismatch,
  that part of the criterion is unmet until #548 clears it.
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
`font-src 'self' data:`, and `img-src 'self' data: blob:`. Cloudscape's CSP
page attributes `style-src 'self'` to "our components" and states it holds for
client-rendered output; the spike shows that for **server-rendered** Cloudscape
output the attribute source (below) still needs `'unsafe-inline'` under
`style-src-attr`. Under the current policy, both shells load with zero CSP
violations and the global-styles package injects no inline `<style>` elements
(Evidence E3).

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
  React uses for `style` props: `style.setProperty(name, value)` or
  `style[name] = value`), so a nonce or hash cannot be the fix for the
  attribute case. Rendering the Cloudscape content on the client therefore
  **does** avoid these `style-src-attr` violations, because React applies the
  `style` props through the CSSOM rather than emitting server `style`
  attributes. Any style that is still server-rendered — such as the current
  loading screen's inline `style` attribute — would continue to need
  `style-src-attr 'unsafe-inline'`.
- `style-src-attr` accepts only `'unsafe-hashes'`, `'unsafe-inline'`, and
  `'report-sample'`; nonces apply to `<style>` and `<script>` elements, never
  to attributes, and hashes with `'unsafe-hashes'` cannot cover dynamic layout
  values.
- A style nonce is not static-compatible: Next 16's CSP guide states that a
  page carrying a nonce must be dynamically rendered, which would undo
  Decision 2's static `/`. Because E3 found no server `<style>` elements, the
  static-compatible option for #553 to verify is `style-src-elem 'self'`
  **without** a nonce (covering any injected `<style>` elements) alongside
  `style-src-attr 'unsafe-inline'` (leaving the server attributes permitted).
  Only if a nonce turns out to be necessary does `/` have to become dynamic,
  which is the cost to weigh then. The alternative remains keeping the shell
  content client-rendered and re-verifying, accepting that any server-rendered
  attribute still requires `style-src-attr 'unsafe-inline'`.

- **Alternatives rejected:** asserting at this ADR that removing react-ui lets
  `style-src` drop `'unsafe-inline'` (the measurement contradicts it, because
  the attributes are server-rendered); applying a nonce or hash to style
  attributes (not valid CSP for attributes); carrying a style nonce on `/`
  (requires dynamic rendering, which conflicts with Decision 2's static `/`);
  loosening the policy further for Cloudscape (unnecessary — the components run
  under `style-src 'self'` for client-rendered output).
- **Consequences:** no CSP change is required to adopt Cloudscape; the policy
  stays as-is through cutover; #553 designs and verifies the static-compatible
  `style-src-elem 'self'` (no nonce) plus `style-src-attr 'unsafe-inline'` split
  in a deployed environment before claiming the policy is tightened, and records
  the dynamic-rendering cost if a nonce proves necessary.

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
| `/_app` shared first-load | 140.0 KiB | no Cloudscape module; growth counted once against the whole-page rule below |
| Every current-UI page (today `/`, measured as `pages[route] ∪ pages['/_app']`) | `/` 822.6 KiB | no Cloudscape module or global CSS; total growth ≤ 2 KiB |
| New Cloudscape shell (chat) | n/a | 500 KiB, **provisional** (see below) |

Rules:

- One enforceable rule covers both current-UI rows, because by the stated
  method every page's measured set already includes `/_app`: for each
  current-UI page, the summed gzip-6 of `pages[route] ∪ pages['/_app']` must
  contain **no** `awsui`/`cloudscape-design` module and must not grow by more
  than **2 KiB** over its baseline. `/_app` growth is therefore counted once —
  inside each page's total — rather than given a separate larger allowance that
  would otherwise double-count against the +0-per-page intent. A script
  (committed by #547) enforces the no-Cloudscape-module check and the 2 KiB
  ceiling.
- The spike measured `/` moving from 822.6 to 823.7 KiB with the second shell
  present (+1.1 KiB), and the content scan found no Cloudscape code in `/`'s
  chunks. That +1.1 KiB is **unexplained**: it is not Cloudscape code, and the
  measurement did not isolate its cause. A separate rebuild with a different
  file set changed the measured gzip total by only a couple of bytes through
  chunk-name churn (Evidence E10), so build-ID churn alone does not account for
  it. The 2 KiB ceiling absorbs this unexplained delta without asserting the
  chunks are byte-identical; #547 investigates the cause when it commits the
  measurement script.
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
- A CSS budget applies too. `globals.css` itself has no `@font-face`, so the
  current-UI CSS carries no embedded fonts; the font weight arrives with
  Cloudscape, whose global stylesheet (`@cloudscape-design/global-styles`)
  embeds eight Open Sans woff2 data URIs (about 190 KB raw). #547 owns the CSS
  budget: it records the gzipped CSS first-load per shell alongside the JS and
  either sets a CSS ceiling or states explicitly that it defers the ceiling to a
  later measurement. The new shell must not pull Cloudscape global CSS into the
  current UI's CSS first-load.
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
and every `.css` entry listed for `pages['/']` and `pages['/admin/users']`
found **no** `awsui`/`cloudscape-design` code or `global-styles` CSS (0 of 11
JS chunks and the page's CSS entries for `/`, 0 of 10 for `/admin/users`); only
the Cloudscape route's chunk contained it (1 of 10). This shows the current UI
loads no Cloudscape JS and no Cloudscape global CSS.

**E3 — production server rendering under CSP (`next start`, headless Chromium).**
With `next start` serving the production build and the production CSP header,
`/` served the current UI (0 `awsui_` in the raw HTML response) by default, and
the Cloudscape shell served under the selector returned **71 `awsui_` substrings
in the raw HTML response** (counted from the server response body, not the live
DOM), with **0** server-rendered `<style>` elements. Both shells loaded under
the production CSP header with 0 violations and **0 React hydration warnings**
in the console for the Cloudscape shell (the harness renders the Cloudscape
page ungated and gates on the client; see decision 3). (In the no-backend harness both
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

**E7 — current-UI equivalence across the `_app` split.** With the element-level
and universal `globals.css` rules scoped under
`:where([data-ga-shell="current"])` and `_app` made shell-aware, the current
UI's `/` computed styles were identical before and after the split:
`body { overflow: hidden }`, body background `rgb(246, 248, 252)`,
`#__next { overflow: hidden }`, Inter font, theme mode `system`, and the focus
outlines — `*:focus { outline: none }` and `*:focus-visible { outline: 2px solid
var(--ga-accent) }` resolving to the same computed `outline` on a focused
control before and after. The raw server HTML differed only in
build-ID/chunk-hash churn and the one added `<html>` `data-ga-shell` attribute;
the server-rendered loading screen was otherwise identical. In the
Cloudscape-direction check, the Cloudscape shell at `/` did **not** inherit the
current UI's page-wide rules: its `body` had `overflow: visible` and a
transparent background, matching Cloudscape's own normalize.css reset rather
than `globals.css`. "Unchanged" is defined as identical computed styles and
rendered structure.

**E8 — same-URL selection flips with no rebuild.** From one build, serving with
the server-only `GBAW_UI_SHELL` unset returned the current UI at `/` (0
`awsui_`); serving the **same** build with `GBAW_UI_SHELL=cloudscape` returned
the Cloudscape shell at `/` (71 `awsui_`) via a Next proxy (`proxy.ts`) rewrite —
`http_code=200`, `num_redirects=0`, URL still `/`. `/` remained statically
prerendered in both the build output and the served responses. No rebuild
occurred between the two cases. The spike reproduced the same flip under both
the deprecated `middleware.ts` and the current `proxy.ts` conventions.

**E9 — syntax-highlighter weight.** The owned `ChatCodeBlock` imports the full
`Prism` build (`import { Prism } from 'react-syntax-highlighter'`), which pulls
refractor 5.0.0's 297 language modules. Bundled and minified in isolation, that
import is 641.0 KiB raw / **226.6 KiB gzip** (raw in KiB, 1,024 bytes, to match
the gzip unit). `PrismLight` with a five-language allowlist (typescript,
python, bash, json, yaml) is 70.9 KiB raw / **24.5 KiB gzip**. The 500 KiB
new-shell ceiling has roughly 177 KiB of headroom over the 323.3 KiB shell
measured in E2/E3, so parity needs `PrismLight` or lazy loading (see the
Bundle-size budget section).

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
then deleted): new `ui/src/proxy.ts` (and the deprecated `ui/src/middleware.ts`
variant), `ui/src/pages/shell-cloudscape.tsx`,
`ui/src/lib/cloudscapeThemeBridge.ts`, and measurement scripts
`ui/pw-computed.mjs`, `ui/pw-isolation.mjs`, `ui/pw-strictcsp.mjs`,
`ui/prism-measure/full.mjs`, `ui/prism-measure/light.mjs`; edits to
`ui/src/pages/_app.tsx` (shell-aware split + `data-ga-shell`),
`ui/src/pages/_document.tsx` (class `_document` mapping
`__NEXT_DATA__.page` to the shell attribute on `<Html>`, defaulting to
`current`),
`ui/src/styles/globals.css` (page-wide rules scoped under
`:where([data-ga-shell="current"])`), and `ui/next.config.mjs` (`transpilePackages`).
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
  the static-compatible `style-src-elem 'self'` (no nonce) plus
  `style-src-attr 'unsafe-inline'` split (or an equivalent) in a deployed
  environment before claiming the policy is tightened; a style nonce would force
  `/` to render dynamically and is not the preferred path.
- **Harness auth.** The spike ran without a live Cognito backend, so the
  authenticated journeys (sign-in, refresh, idle warning) were not exercised
  end to end; #547 and #548 validate those against both shells.
- **Full chat parity bundle.** The 323.3 KiB new-shell measurement is a minimal
  client-rendered conversation. The 500 KiB ceiling is provisional and must be
  re-measured as markdown, charts, and code-block behavior are added, with
  `PrismLight` or lazy loading for the highlighter (#549, #550).

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
