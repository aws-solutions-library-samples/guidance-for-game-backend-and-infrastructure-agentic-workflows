/**
 * Isolation between shells rests on scoping the current UI's page-wide resets so
 * they never reach a page rendered in another shell. Every element-level and
 * universal rule in globals.css MUST be nested under
 * :where([data-ga-shell="current"]) — which keeps specificity at (0,0,0) so the
 * current UI is unchanged — while the prefers-reduced-motion accessibility
 * safeguard MUST stay global so it applies to both shells.
 *
 * This test reads the real production stylesheet from disk (not the Jest CSS
 * mock) so it exercises exactly what ships.
 */

import { readFileSync } from 'fs';
import { join } from 'path';

const GLOBALS_CSS = readFileSync(join(__dirname, '..', '..', 'styles', 'globals.css'), 'utf8');
const SCOPE = ":where([data-ga-shell='current'])";

describe('globals.css shell scoping', () => {
  it.each([
    [':where([data-ga-shell=\'current\']) html,'],
    [`${SCOPE} a {`],
    [`${SCOPE} * {`],
    [`${SCOPE} #__next {`],
    [`${SCOPE} button {`],
    [`${SCOPE} ::selection {`],
    [`${SCOPE} *:focus {`],
    [`${SCOPE} *:focus-visible {`],
  ])('scopes the page-wide rule %s under the current shell', (fragment) => {
    expect(GLOBALS_CSS).toContain(fragment);
  });

  it('does not leave an unscoped universal box-sizing reset', () => {
    // A bare "* { box-sizing: border-box }" would reach every shell. After
    // scoping, the only universal selector is the scoped one.
    expect(GLOBALS_CSS).not.toMatch(/(^|\})\s*\*\s*\{\s*box-sizing/m);
  });

  it('keeps the prefers-reduced-motion safeguard global (unscoped)', () => {
    // The block must appear with the bare universal selectors, not nested under
    // the shell scope on the same selector line.
    expect(GLOBALS_CSS).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\*,\s*\*::before,\s*\*::after\s*\{/);
    expect(GLOBALS_CSS).not.toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*:where\(\[data-ga-shell/);
  });
});
