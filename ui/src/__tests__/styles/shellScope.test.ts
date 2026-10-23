/**
 * Isolation between shells rests on scoping the current UI's page-wide resets so
 * they never reach a page rendered in another shell, while keeping the current
 * UI's own `<html>`/`<body>` resets intact. The root reset MUST target
 * `html[data-ga-shell="current"]` (the attribute sits on <html> itself, so a
 * descendant combinator would never match <html>), every scoped rule MUST stay
 * at specificity (0,0,0) via :where(), and the prefers-reduced-motion
 * accessibility safeguard MUST stay global so it applies to both shells.
 *
 * The checks below evaluate real selector matches with Element.matches() against
 * a current-shell and a Cloudscape-shell <html>, rather than substring-matching
 * the stylesheet text, so a selector that parses but matches nothing fails.
 */

import { readFileSync } from 'fs';
import { join } from 'path';

const GLOBALS_CSS = readFileSync(join(__dirname, '..', '..', 'styles', 'globals.css'), 'utf8');

/** Strip comments and extract the selector list of every top-level rule. */
function ruleSelectors(css: string): string[] {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const selectors: string[] = [];
  // Match "<selectors> { ... }" at the top level (good enough for this flat
  // stylesheet; @media blocks are handled separately below).
  const ruleRe = /([^{}]+)\{[^{}]*\}/g;
  let match: RegExpExecArray | null;
  while ((match = ruleRe.exec(withoutComments)) !== null) {
    const selector = match[1].trim();
    if (selector && !selector.startsWith('@') && !selector.startsWith(':root')) {
      selectors.push(selector);
    }
  }
  return selectors;
}

/** Does any individual selector in the stylesheet match the given element? */
function anySelectorMatches(element: Element, predicate: (sel: string) => boolean): boolean {
  return ruleSelectors(GLOBALS_CSS)
    .flatMap((group) => group.split(','))
    .map((sel) => sel.trim())
    .filter(predicate)
    .some((sel) => {
      try {
        return element.matches(sel);
      } catch {
        return false;
      }
    });
}

function htmlWithShell(shell: string): HTMLElement {
  const html = document.createElement('html');
  html.setAttribute('data-ga-shell', shell);
  return html;
}

describe('globals.css shell scoping', () => {
  it('applies the root reset to a current-shell <html>', () => {
    const html = htmlWithShell('current');
    // The reset block declares background/color/font on the root; assert <html>
    // actually matches the selector that carries them.
    expect(
      anySelectorMatches(html, (sel) => /html\[data-ga-shell='current'\]/.test(sel)),
    ).toBe(true);
  });

  it('does not apply the root reset to a Cloudscape-shell <html>', () => {
    const html = htmlWithShell('cloudscape');
    expect(
      anySelectorMatches(html, (sel) => /data-ga-shell='current'/.test(sel)),
    ).toBe(false);
  });

  it('includes the current-shell <html> in the box-sizing reset', () => {
    const html = htmlWithShell('current');
    const css = GLOBALS_CSS.replace(/\/\*[\s\S]*?\*\//g, '');
    // Isolate the selector group of the rule whose body sets box-sizing.
    const boxSizingRule = /([^{}]+)\{\s*box-sizing:\s*border-box;?\s*\}/.exec(css);
    expect(boxSizingRule).not.toBeNull();
    const matched = (boxSizingRule as RegExpExecArray)[1]
      .split(',')
      .map((s) => s.trim())
      .some((sel) => {
        try {
          return html.matches(sel);
        } catch {
          return false;
        }
      });
    expect(matched).toBe(true);
  });

  it('keeps every scoped selector at zero specificity via :where()', () => {
    const scoped = ruleSelectors(GLOBALS_CSS)
      .flatMap((group) => group.split(','))
      .map((sel) => sel.trim())
      .filter((sel) => sel.includes("data-ga-shell='current'"));
    expect(scoped.length).toBeGreaterThan(0);
    // Each scoped selector must wrap the shell attribute in :where(...) so it
    // contributes zero specificity.
    for (const sel of scoped) {
      expect(sel).toMatch(/:where\([^)]*data-ga-shell='current'[^)]*\)/);
    }
  });

  it('does not leave an unscoped universal box-sizing reset', () => {
    // A bare "* { box-sizing: border-box }" would reach every shell.
    expect(GLOBALS_CSS).not.toMatch(/(^|\})\s*\*\s*\{\s*box-sizing/m);
  });

  it('keeps the prefers-reduced-motion safeguard global (unscoped)', () => {
    expect(GLOBALS_CSS).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\*,\s*\*::before,\s*\*::after\s*\{/);
    expect(GLOBALS_CSS).not.toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*:where\(\[data-ga-shell/);
  });
});
