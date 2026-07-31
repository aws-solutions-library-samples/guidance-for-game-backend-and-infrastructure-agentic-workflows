/**
 * Browser test for #253 — the new chat button renders in the live chat and is
 * clickable (z-index must clear CopilotKit's message stacking, which is why a
 * DOM-only unit test isn't sufficient here).
 * Tags: @e2e @visual @fast
 */

import { test, expect } from '@playwright/test';

test.describe('#253 - new chat button', { tag: ['@e2e', '@visual', '@fast'] }, () => {
  test('button renders inside the chat and is clickable', async ({ page }) => {
    await page.goto('/');
    const button = page.getByRole('button', { name: /new chat/i });
    await expect(button).toBeVisible();
    await button.click();
    // Post-click the app must remain functional (no crash) with the input usable.
    await expect(page.locator('.copilotKitInput textarea')).toBeVisible();
  });
});
