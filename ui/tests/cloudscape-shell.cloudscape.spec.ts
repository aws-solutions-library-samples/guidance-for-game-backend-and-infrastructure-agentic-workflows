/**
 * The Cloudscape shell, exercised through the server-side selector.
 *
 * These tests run only under `playwright.cloudscape.config.ts`, which serves a
 * production build with `GBAW_UI_SHELL=cloudscape` so `/` is rewritten to the
 * Cloudscape shell at the same URL. They assert the shell renders an empty,
 * authenticated, accessible layout in light and dark mode with no axe
 * violations. The default Playwright config ignores this file, so the existing
 * current-shell suite is unaffected.
 *
 * Tags: @e2e @cloudscape @a11y
 */

import AxeBuilder from '@axe-core/playwright';
import { test, expect } from '@playwright/test';
import { cloudscapeShell } from './helpers/shells';

async function setTheme(page: import('@playwright/test').Page, mode: 'light' | 'dark') {
  await page.evaluate((m) => {
    window.localStorage.setItem('game-agent-theme', m);
    window.dispatchEvent(new Event('game-agent-theme-change'));
  }, mode);
}

test.describe('Cloudscape shell', { tag: ['@e2e', '@cloudscape', '@a11y'] }, () => {
  test('serves the Cloudscape shell at / with no redirect', async ({ page }) => {
    const response = await page.goto('/');
    expect(response?.status()).toBe(200);
    expect(new URL(page.url()).pathname).toBe('/');
    await cloudscapeShell.expectShellAttribute(page);
  });

  test('renders an accessible empty layout in light mode', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await setTheme(page, 'light');
    await expect.poll(() => cloudscapeShell.resolvedTheme(page)).toBe('light');
    await expect(cloudscapeShell.mainLandmark(page)).toBeVisible();

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toEqual([]);
  });

  test('renders an accessible empty layout in dark mode', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await setTheme(page, 'dark');
    await expect.poll(() => cloudscapeShell.resolvedTheme(page)).toBe('dark');
    await expect(cloudscapeShell.mainLandmark(page)).toBeVisible();

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toEqual([]);
  });

  test('loads no CopilotKit chat markup (empty foundation shell)', async ({ page }) => {
    await cloudscapeShell.goto(page);
    await expect(page.locator('.copilotKitMessages')).toHaveCount(0);
  });
});
