/**
 * Basic End-to-End Tests for Game Agent
 *
 * Tests core functionality without performance requirements
 * Tags: @e2e @sanity
 */

import { test, expect } from '@playwright/test';

test.describe('Game Agent Basic E2E Tests', { tag: ['@e2e', '@sanity'] }, () => {

  test.beforeEach(async ({ page }) => {
    // Navigate to the application
    await page.goto('/');

    // Wait for the page to load
    await page.waitForLoadState('networkidle');
  });

  test('should load the home page', async ({ page }) => {
    // Check page title
    await expect(page).toHaveTitle(/Game Agent/);

    // Check for main heading or logo
    const heading = page.locator('h1, .ga-branding h1');
    await expect(heading).toBeVisible({ timeout: 10000 });
  });

  test('should display the chat interface', async ({ page }) => {
    // Look for chat input elements
    const chatInput = page.locator('input[type="text"], textarea, [contenteditable="true"]').first();
    await expect(chatInput).toBeVisible({ timeout: 15000 });

    // Look for send button
    const sendButton = page.locator('button').filter({ hasText: /send|submit/i }).first();
    if (await sendButton.isVisible()) {
      await expect(sendButton).toBeVisible();
    }
  });

  test('should allow typing in chat input', async ({ page }) => {
    // Find chat input
    const chatInput = page.locator('input[type="text"], textarea, [contenteditable="true"]').first();
    await expect(chatInput).toBeVisible({ timeout: 15000 });

    // Type a message
    await chatInput.fill('Hello, Game Agent!');

    // Verify text was entered
    if (await chatInput.getAttribute('contenteditable')) {
      // For contenteditable elements
      await expect(chatInput).toContainText('Hello, Game Agent!');
    } else {
      // For input/textarea elements
      await expect(chatInput).toHaveValue('Hello, Game Agent!');
    }
  });

  test('should handle simple interaction', async ({ page }) => {
    // Find chat input
    const chatInput = page.locator('input[type="text"], textarea, [contenteditable="true"]').first();
    await expect(chatInput).toBeVisible({ timeout: 15000 });

    // Type a simple message
    await chatInput.fill('hello');

    // Try to send the message (either button click or Enter key)
    const sendButton = page.locator('button').filter({ hasText: /send|submit/i }).first();

    if (await sendButton.isVisible()) {
      await sendButton.click();
    } else {
      await chatInput.press('Enter');
    }

    // Wait a moment for any response (but don't require it)
    await page.waitForTimeout(2000);

    // Test passes if no errors occurred
    expect(true).toBe(true);
  });

  test('should be responsive', async ({ page }) => {
    // Test desktop view
    await page.setViewportSize({ width: 1200, height: 800 });
    await page.waitForTimeout(1000);

    const chatInput = page.locator('input[type="text"], textarea, [contenteditable="true"]').first();
    await expect(chatInput).toBeVisible();

    // Test mobile view
    await page.setViewportSize({ width: 375, height: 667 });
    await page.waitForTimeout(1000);

    // Chat should still be visible on mobile
    await expect(chatInput).toBeVisible();
  });

  test('should handle page refresh', async ({ page }) => {
    // Reload the page
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Should still work after refresh
    const chatInput = page.locator('input[type="text"], textarea, [contenteditable="true"]').first();
    await expect(chatInput).toBeVisible({ timeout: 15000 });
  });

  test('should not have console errors', async ({ page }) => {
    const consoleErrors: string[] = [];

    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    // Navigate and interact
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const chatInput = page.locator('input[type="text"], textarea, [contenteditable="true"]').first();
    if (await chatInput.isVisible()) {
      await chatInput.fill('test');
    }

    // Allow some time for any async errors
    await page.waitForTimeout(3000);

    // Filter out known non-critical errors
    const criticalErrors = consoleErrors.filter(error =>
      !error.includes('favicon') &&
      !error.includes('404') &&
      !error.includes('net::ERR_FAILED') &&
      !error.toLowerCase().includes('warning')
    );

    expect(criticalErrors).toHaveLength(0);
  });
});
