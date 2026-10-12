/**
 * Per-shell page objects.
 *
 * The existing specs hard-code the current shell's `.copilotKit*` and `.ga-*`
 * selectors, so running "against both shells" means a page object per shell
 * rather than a configuration flag. Each page object exposes the same intent
 * (load the shell, find the main landmark, read the resolved theme) over the
 * selectors that shell actually renders, so a spec can target either shell by
 * choosing a page object.
 */

import { expect, Page } from '@playwright/test';

export type ShellName = 'current' | 'cloudscape';

export interface ShellPage {
  readonly name: ShellName;
  /** Navigate to `/` and wait for the shell to be interactive. */
  goto(page: Page): Promise<void>;
  /** The main landmark locator for this shell. */
  mainLandmark(page: Page): ReturnType<Page['locator']>;
  /** Assert the shell attribute on <html> matches this shell. */
  expectShellAttribute(page: Page): Promise<void>;
  /** The resolved theme (`light` or `dark`) read from <html>. */
  resolvedTheme(page: Page): Promise<string | null>;
}

async function readShellAttribute(page: Page): Promise<string | null> {
  return page.evaluate(() => document.documentElement.getAttribute('data-ga-shell'));
}

async function readTheme(page: Page): Promise<string | null> {
  return page.evaluate(() => document.documentElement.getAttribute('data-theme'));
}

/** Page object for the current (default) shell. */
export const currentShell: ShellPage = {
  name: 'current',
  async goto(page) {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('.ga-layout')).toBeVisible({ timeout: 15000 });
  },
  mainLandmark(page) {
    return page.locator('main.ga-main, main');
  },
  async expectShellAttribute(page) {
    expect(await readShellAttribute(page)).toBe('current');
  },
  resolvedTheme: readTheme,
};

/** Page object for the Cloudscape shell. */
export const cloudscapeShell: ShellPage = {
  name: 'cloudscape',
  async goto(page) {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    // The Cloudscape shell renders a visible <h1> inside AppLayout's content.
    await expect(page.getByRole('heading', { name: 'Game Agent', level: 1 })).toBeVisible({
      timeout: 15000,
    });
  },
  mainLandmark(page) {
    return page.getByRole('main').first();
  },
  async expectShellAttribute(page) {
    expect(await readShellAttribute(page)).toBe('cloudscape');
  },
  resolvedTheme: readTheme,
};

export const SHELLS: Record<ShellName, ShellPage> = {
  current: currentShell,
  cloudscape: cloudscapeShell,
};
