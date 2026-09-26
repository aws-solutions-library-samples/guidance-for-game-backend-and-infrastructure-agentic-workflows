/**
 * Blocker 4 (headless browser): while a refresh disables both dialog actions,
 * focus must stay inside the modal. jsdom's Tab handling does not move focus
 * between elements the way Chromium does, so this runs the SHIPPED focus-trap
 * logic in a real browser and presses real Tab keys.
 *
 * Tags: @browser @idle
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { join } from 'path';
import { handleFocusTrapKeydown } from '../../src/utils/dialogFocusTrap';

const GLOBALS_CSS = readFileSync(
  join(__dirname, '..', '..', 'src', 'styles', 'globals.css'),
  'utf8',
);

const FIXTURE = `
  <input id="outside" type="text" />
  <div class="ga-idle-overlay">
    <div id="dialog" role="dialog" aria-modal="true" tabindex="-1" aria-label="Still there?">
      <h2>Still there?</h2>
      <button id="stay" type="button">Stay signed in</button>
      <button id="signout" type="button">Sign out</button>
    </div>
  </div>
`;

async function installTrap(page: import('@playwright/test').Page) {
  await page.evaluate((trapSrc) => {
    const handle = new Function('return (' + trapSrc + ')')() as (
      container: HTMLElement,
      event: KeyboardEvent,
    ) => boolean;
    const dialog = document.getElementById('dialog') as HTMLElement;
    dialog.addEventListener('keydown', (e) => handle(dialog, e));
  }, handleFocusTrapKeydown.toString());
}

test.describe('idle warning dialog focus trap (real browser)', { tag: ['@browser', '@idle'] }, () => {
  test('Tab stays inside the dialog while both actions are disabled (busy)', async ({ page }) => {
    await page.setContent(FIXTURE);
    await installTrap(page);

    // Enter the busy state: disable BOTH actions and focus the container, as the
    // component does when a refresh is in flight.
    await page.evaluate(() => {
      (document.getElementById('stay') as HTMLButtonElement).disabled = true;
      (document.getElementById('signout') as HTMLButtonElement).disabled = true;
      (document.getElementById('dialog') as HTMLElement).focus();
    });

    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');

    const activeId = await page.evaluate(() => document.activeElement?.id);
    // Focus must remain within the dialog (the focusable container), never the
    // outside input behind the modal.
    expect(activeId).toBe('dialog');
  });

  test('normal two-button wrapping still works when actions are enabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await installTrap(page);

    await page.focus('#signout');
    await page.keyboard.press('Tab'); // from last -> wraps to first
    expect(await page.evaluate(() => document.activeElement?.id)).toBe('stay');

    await page.focus('#stay');
    await page.keyboard.press('Shift+Tab'); // from first -> wraps to last
    expect(await page.evaluate(() => document.activeElement?.id)).toBe('signout');
  });

  test('focus does not escape to the background input across the busy transition', async ({ page }) => {
    await page.setContent(FIXTURE);
    await installTrap(page);

    await page.focus('#stay');
    // Refresh starts: disable both. The component moves focus to the container.
    await page.evaluate(() => {
      (document.getElementById('stay') as HTMLButtonElement).disabled = true;
      (document.getElementById('signout') as HTMLButtonElement).disabled = true;
      (document.getElementById('dialog') as HTMLElement).focus();
    });
    await page.keyboard.press('Tab');
    const activeId = await page.evaluate(() => document.activeElement?.id);
    expect(activeId).not.toBe('outside');
    expect(activeId).toBe('dialog');
  });

  test('the busy-state fallback focus target shows a VISIBLE focus indicator under production CSS (#310, Blocker 2)', async ({ page }) => {
    await page.setContent(FIXTURE);
    // Load the REAL production stylesheet so we measure what ships, not a mock.
    await page.addStyleTag({ content: GLOBALS_CSS });
    // Give the dialog its production class so the .ga-idle-dialog rules apply.
    await page.evaluate(() => {
      const dialog = document.getElementById('dialog') as HTMLElement;
      dialog.classList.add('ga-idle-dialog');
    });

    // Enter the busy state: disable BOTH actions and move keyboard focus to the
    // container, exactly as the component does while a refresh is in flight.
    await page.evaluate(() => {
      (document.getElementById('stay') as HTMLButtonElement).disabled = true;
      (document.getElementById('signout') as HTMLButtonElement).disabled = true;
    });
    // Use keyboard interaction so :focus-visible matches (not a pointer focus).
    await page.keyboard.press('Tab');
    await page.evaluate(() => (document.getElementById('dialog') as HTMLElement).focus());

    const indicator = await page.evaluate(() => {
      const dialog = document.getElementById('dialog') as HTMLElement;
      const style = getComputedStyle(dialog);
      return {
        outlineStyle: style.outlineStyle,
        outlineWidth: style.outlineWidth,
        outlineColor: style.outlineColor,
        boxShadow: style.boxShadow,
      };
    });

    // The container must render a visible ring: a real outline (non-none style
    // with non-zero width) OR an equivalent box-shadow. A bare outline:none with
    // no replacement — the pre-fix behavior — would fail this assertion.
    const outlineWidthPx = parseFloat(indicator.outlineWidth || '0');
    const hasVisibleOutline = indicator.outlineStyle !== 'none' && outlineWidthPx > 0;
    const hasVisibleBoxShadow = !!indicator.boxShadow && indicator.boxShadow !== 'none';
    expect(hasVisibleOutline || hasVisibleBoxShadow).toBe(true);
  });
});
