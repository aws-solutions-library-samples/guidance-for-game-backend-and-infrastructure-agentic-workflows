/**
 * Smoke Tests for Game Agent
 *
 * Quick sanity checks to verify the system is basically working.
 * These should be FAST (<30s total) and cover only critical paths.
 *
 * Tags: @e2e @smoke @fast
 */

import { test, expect } from '@playwright/test';

test.describe('Game Agent Smoke Tests', { tag: ['@e2e', '@smoke', '@fast'] }, () => {

  test('should load the application', async ({ page }) => {
    // Navigate to the application
    await page.goto('/');

    // Wait for page to load (use domcontentloaded for speed)
    await page.waitForLoadState('domcontentloaded');

    // Check page title
    await expect(page).toHaveTitle(/Game Agent/);
  });

  test('should display chat interface', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // Look for chat input - the core functionality
    const chatInput = page.locator('textarea, input[type="text"]').first();
    await expect(chatInput).toBeVisible({ timeout: 10000 });
  });

  test('should allow typing in chat', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // Find chat input
    const chatInput = page.locator('textarea, input[type="text"]').first();
    await expect(chatInput).toBeVisible({ timeout: 10000 });

    // Type a message
    await chatInput.fill('test');

    // Verify text was entered
    await expect(chatInput).toHaveValue('test');
  });
});
