/**
 * The Cloudscape shell, exercised through the server-side selector.
 *
 * These tests run only under `playwright.cloudscape.config.ts`, which serves
 * `next dev` with `GBAW_UI_SHELL=cloudscape` so `/` is rewritten to the
 * Cloudscape shell at the same URL. They assert the shell renders an empty,
 * authenticated, accessible layout that is really light in light mode and
 * really dark in dark mode (reading a rendered Cloudscape result, not just the
 * theme attribute `ThemeProvider` sets on its own), with no axe violations, and
 * that a client-side navigation across the shell boundary never mixes the two
 * design systems. The default Playwright config ignores this file, so the
 * existing current-shell suite is unaffected.
 *
 * `next dev` does not emit the production security headers, so these specs do
 * not exercise the production CSP; that is validated separately (see the ADR's
 * "Harness auth" open risk).
 *
 * Tags: @e2e @cloudscape @a11y
 */

import AxeBuilder from '@axe-core/playwright';
import { test, expect } from '@playwright/test';
import type { NextRouter } from 'next/router';
import { cloudscapeContentBackground, cloudscapeShell, gaTokenOnBody } from './helpers/shells';

async function setTheme(page: import('@playwright/test').Page, mode: 'light' | 'dark' | 'system') {
  await page.evaluate((m) => {
    window.localStorage.setItem('game-agent-theme', m);
    window.dispatchEvent(new Event('game-agent-theme-change'));
  }, mode);
}

/**
 * Whether an error comes from a page read that raced a full document load. The
 * shell-boundary reload replaces the document, which destroys the execution
 * context that a pending `page.evaluate` was running in.
 */
function isDocumentReplacedError(error: unknown): boolean {
  return error instanceof Error && /Execution context was destroyed/.test(error.message);
}

/**
 * Read the document's `data-ga-shell` attribute, or `null` while the document is
 * being replaced, so a poll keeps waiting through a full document load instead
 * of failing on the destroyed context.
 */
async function documentShellAttribute(page: import('@playwright/test').Page): Promise<string | null> {
  try {
    return await page.evaluate(() => document.documentElement.getAttribute('data-ga-shell'));
  } catch (error) {
    if (isDocumentReplacedError(error)) {
      return null;
    }
    throw error;
  }
}

/** Parse an `rgb()/rgba()` or `#rrggbb` color into a perceived luminance (0 dark … 255 light). */
function luminanceOf(color: string): number {
  const trimmed = color.trim();
  let r: number;
  let g: number;
  let b: number;
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(trimmed);
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].replace(/(.)/g, '$1$1') : hex[1];
    r = parseInt(h.slice(0, 2), 16);
    g = parseInt(h.slice(2, 4), 16);
    b = parseInt(h.slice(4, 6), 16);
  } else {
    const parts = trimmed.match(/\d+(\.\d+)?/g);
    if (!parts || parts.length < 3) {
      throw new Error(`unparseable color: ${color}`);
    }
    [r, g, b] = parts.map(Number);
  }
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test.describe('Cloudscape shell', { tag: ['@e2e', '@cloudscape', '@a11y'] }, () => {
  test('serves the Cloudscape shell at / with no redirect', async ({ page }) => {
    const response = await page.goto('/');
    expect(response?.status()).toBe(200);
    expect(new URL(page.url()).pathname).toBe('/');
    await cloudscapeShell.expectShellAttribute(page);
  });

  test('renders a visibly light layout in light mode', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await setTheme(page, 'light');
    await expect.poll(() => cloudscapeShell.resolvedTheme(page)).toBe('light');
    await expect(cloudscapeShell.mainLandmark(page)).toBeVisible();

    // The rendered Cloudscape content background must be light, and the bridged
    // --ga-bg on <body> must resolve to a light value — not the light fallback
    // that a bridge written to the wrong element would leave behind.
    await expect.poll(() => cloudscapeContentBackground(page).then(luminanceOf)).toBeGreaterThan(200);
    const bg = await gaTokenOnBody(page, '--ga-bg');
    expect(bg).not.toBe('');

    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    expect(results.violations).toEqual([]);
  });

  test('renders a visibly dark layout in dark mode', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await setTheme(page, 'dark');
    await expect.poll(() => cloudscapeShell.resolvedTheme(page)).toBe('dark');
    await expect(cloudscapeShell.mainLandmark(page)).toBeVisible();

    // Dark mode must actually render dark. Assert the rendered Cloudscape
    // content background is dark, and the bridged --ga-bg on <body> resolves to
    // a dark value.
    await expect.poll(() => cloudscapeContentBackground(page).then(luminanceOf)).toBeLessThan(80);
    const bg = await gaTokenOnBody(page, '--ga-bg');
    expect(luminanceOf(bg)).toBeLessThan(80);

    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    expect(results.violations).toEqual([]);
  });

  test('follows the OS color scheme in system mode', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await setTheme(page, 'system');

    await page.emulateMedia({ colorScheme: 'dark' });
    await expect.poll(() => cloudscapeShell.resolvedTheme(page)).toBe('dark');
    await expect.poll(() => cloudscapeContentBackground(page).then(luminanceOf)).toBeLessThan(80);

    await page.emulateMedia({ colorScheme: 'light' });
    await expect.poll(() => cloudscapeShell.resolvedTheme(page)).toBe('light');
    await expect.poll(() => cloudscapeContentBackground(page).then(luminanceOf)).toBeGreaterThan(200);
  });

  test('loads no CopilotKit chat markup (empty foundation shell)', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await expect(page.locator('.copilotKitMessages')).toHaveCount(0);
  });

  test('does not mix design systems across a client-side navigation and back', async ({ page }) => {
    // Start in the Cloudscape shell at `/`.
    await cloudscapeShell.goto(page);
    await cloudscapeShell.expectShellAttribute(page);

    // The current shell has no secondary product page after the admin UI was
    // removed. Use Next's real 404 page, then its hydrated client router, to
    // cross the boundary without introducing a route just for this test.
    await page.goto('/cloudscape-boundary-missing-page');
    await expect(page.getByRole('heading', { name: '404', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-ga-shell'))).toBe('current');
    await page.waitForFunction(() => {
      const next = (window as Window & { next?: { router?: Pick<NextRouter, 'isReady'> } }).next;
      return next?.router?.isReady === true;
    });

    // Count document-level requests triggered by the router push. The push is a
    // client-side navigation to `/`; the shell-boundary reload must turn it into
    // exactly one full document load so the Cloudscape document is served (never
    // the Cloudscape page inside the current-shell document). Removing the hook
    // leaves the current-shell document in place and makes zero document
    // requests, so this count is what fails without the fix.
    let documentRequests = 0;
    const countDocuments = (request: import('@playwright/test').Request) => {
      if (request.resourceType() === 'document') {
        documentRequests += 1;
      }
    };
    page.on('request', countDocuments);

    // Use the actual Pages Router; a full-navigation fallback would conceal a
    // broken boundary hook and make the document-request assertion meaningless.
    // The push is expected to trigger the boundary reload, which can destroy
    // this evaluation's context before it returns; any other error fails.
    await page
      .evaluate(async () => {
        const next = (window as Window & { next?: { router?: Pick<NextRouter, 'push'> } }).next;
        if (!next?.router) {
          throw new Error('The hydrated Pages Router is required for the shell-boundary test');
        }
        await next.router.push('/');
      })
      .catch((error: unknown) => {
        if (!isDocumentReplacedError(error)) {
          throw error;
        }
      });

    // Wait until the Cloudscape document is in place. The shell attribute only
    // flips to `cloudscape` after the full reload loads the Cloudscape
    // document, so polling it (rather than the shared "Game Agent" heading,
    // which the current shell's own navigation also renders) guarantees the
    // reload has completed before the count is read. A read that races the
    // reload returns `null` and the poll retries.
    await expect.poll(() => documentShellAttribute(page), { timeout: 15000 }).toBe('cloudscape');
    page.off('request', countDocuments);
    expect(documentRequests).toBe(1);
    // The current shell's CopilotKit markup must not be present in the
    // Cloudscape document.
    await expect(page.locator('.copilotKitMessages')).toHaveCount(0);

    // Browser Back returns to the missing page; it must render as the current
    // shell with no Cloudscape markup left attached.
    await page.goBack();
    await page.waitForLoadState('load');
    await expect.poll(() => documentShellAttribute(page)).toBe('current');
    await expect.poll(() => page.locator('[class*="awsui_"]').count()).toBe(0);
  });
});
