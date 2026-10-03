/**
 * Blocker 2 (#310): the busy-state fallback focus target must keep a VISIBLE
 * keyboard focus indicator in production CSS.
 *
 * While a "Stay signed in" refresh is in flight both dialog actions are
 * disabled, so `IdleWarningDialog` moves focus to the dialog CONTAINER
 * (`.ga-idle-dialog`, `tabindex="-1"`) to keep the focus trap intact. If the
 * container's `:focus` rule suppresses the outline with `outline: none` and
 * offers no visible replacement, a keyboard user sees NO focus indicator on the
 * only focusable element in the modal — a WCAG 2.4.7 (Focus Visible) failure.
 *
 * This test loads and inspects the REAL production stylesheet (not a mock or a
 * reimplementation) and fails if the fallback focus target's visible indicator
 * is removed. It intentionally does not go through the Jest CSS module mock:
 * it reads the file from disk so it exercises exactly what ships.
 */

import { readFileSync } from 'fs';
import { join } from 'path';

const GLOBALS_CSS = join(__dirname, '..', '..', 'styles', 'globals.css');

/** Extract the declaration body of the first rule whose selector list contains `selector`. */
function ruleBody(css: string, selector: string): string | null {
  // Match "<selectors> { ... }" where the selector list contains our selector
  // as a whole token (bounded by start, comma, or whitespace).
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`(^|})[^{}]*(?:^|[\\s,])${escaped}[\\s,{][^{]*\\{([^}]*)\\}`, 'm');
  const match = re.exec(css);
  return match ? match[2] : null;
}

/** Parse "prop: value" declarations from a rule body into a lowercased map (last wins). */
function declarations(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const decl of body.split(';')) {
    const idx = decl.indexOf(':');
    if (idx === -1) continue;
    const prop = decl.slice(0, idx).trim().toLowerCase();
    const value = decl.slice(idx + 1).trim().toLowerCase();
    if (prop) out[prop] = value;
  }
  return out;
}

/**
 * A declaration set provides a visible focus indicator when it either:
 *  - keeps a non-`none` outline (width + style), or
 *  - draws an equivalent high-contrast ring via box-shadow / border.
 */
function providesVisibleFocusIndicator(decls: Record<string, string>): boolean {
  const outline = decls['outline'];
  const outlineStyle = decls['outline-style'];
  const outlineWidth = decls['outline-width'];
  const boxShadow = decls['box-shadow'];
  const border = decls['border'];
  const borderColor = decls['border-color'];

  const hasVisibleOutline =
    (typeof outline === 'string' && outline !== 'none' && outline !== '0' && /\d/.test(outline)) ||
    (outlineStyle && outlineStyle !== 'none' && outlineWidth && outlineWidth !== '0');

  const hasVisibleBoxShadow = typeof boxShadow === 'string' && boxShadow !== 'none' && /\d/.test(boxShadow);

  const hasVisibleBorder =
    (typeof border === 'string' && border !== 'none' && /\d/.test(border)) ||
    (typeof borderColor === 'string' && borderColor.length > 0);

  return Boolean(hasVisibleOutline || hasVisibleBoxShadow || hasVisibleBorder);
}

describe('idle dialog fallback focus target — visible focus indicator (production CSS)', () => {
  const css = readFileSync(GLOBALS_CSS, 'utf8');

  it('the .ga-idle-dialog fallback focus target has a :focus rule', () => {
    expect(ruleBody(css, '.ga-idle-dialog:focus')).not.toBeNull();
  });

  it('does not remove the visible focus indicator from the busy-state fallback target', () => {
    const body = ruleBody(css, '.ga-idle-dialog:focus');
    expect(body).not.toBeNull();
    const decls = declarations(body as string);

    // A bare `outline: none` with no visible replacement removes the ONLY focus
    // affordance for a keyboard user during the busy state. Fail closed.
    const suppressesOutline = decls['outline'] === 'none' || decls['outline'] === '0';
    if (suppressesOutline) {
      expect(providesVisibleFocusIndicator(decls)).toBe(true);
    }

    // Regardless of how it is expressed, the rule must yield a visible indicator.
    expect(providesVisibleFocusIndicator(decls)).toBe(true);
  });

  it('uses an existing theme token for the focus indicator color', () => {
    const body = ruleBody(css, '.ga-idle-dialog:focus');
    expect(body).not.toBeNull();
    // The high-contrast indicator should reference a defined theme token so it
    // adapts to light/dark themes, consistent with the global focus-visible ring.
    expect(body as string).toMatch(/var\(--ga-[a-z-]+/);
  });
});
