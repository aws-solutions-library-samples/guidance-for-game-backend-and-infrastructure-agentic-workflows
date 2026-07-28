/**
 * Browser regression tests for #251 — chat visibility on laptop-height viewports.
 * All pre-existing breakpoints were width-only; these tests pin the height-based
 * compaction at a 14" MacBook Pro logical viewport.
 * Tags: @e2e @visual @fast
 */

import { test, expect } from '@playwright/test';

// 14" MacBook Pro logical viewport (default scaled resolution).
const LAPTOP_VIEWPORT = { width: 1512, height: 982 };

test.describe('#251 - chat visibility on laptop-height viewport', { tag: ['@e2e', '@visual', '@fast'] }, () => {
  test.use({ viewport: LAPTOP_VIEWPORT });

  test('message area gets the majority of the viewport height', async ({ page }) => {
    await page.goto('/');
    await page.waitForSelector('.copilotKitMessages', { timeout: 10000 });

    const messagesBox = await page.locator('.copilotKitMessages').boundingBox();
    expect(messagesBox).not.toBeNull();

    // Pre-fix, fixed chrome ate ~52% of the viewport leaving ~42% for chat.
    // Require a majority share so regressions get caught.
    expect(messagesBox!.height).toBeGreaterThan(LAPTOP_VIEWPORT.height * 0.5);
  });

  test('welcome message start is visible without scrolling up', async ({ page }) => {
    await page.goto('/');
    const messages = page.locator('.copilotKitMessages');
    await expect(messages).toBeVisible();
    // Give CopilotKit's initial-message render + auto-scroll a beat.
    await page.waitForTimeout(500);

    // The container must not be scrolled: scrollTop 0 means the greeting's
    // first line is on-screen (pre-fix the long welcome auto-scrolled to its
    // tail and users had to scroll UP to see how the message started).
    const scrollTop = await messages.evaluate((el) => el.scrollTop);
    expect(scrollTop).toBe(0);
  });
});
