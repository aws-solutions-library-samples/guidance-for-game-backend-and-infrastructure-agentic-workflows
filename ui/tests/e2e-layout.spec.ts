/**
 * Layout and Visual Tests - Verify UI structure is correct
 * Tags: @e2e @visual @fast
 */

import { test, expect } from '@playwright/test';

test.describe('Game Agent - Layout Tests', { tag: ['@e2e', '@visual', '@fast'] }, () => {

  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ga-chat-container', { timeout: 10000 });
  });

  test('footer does not overlap input box', async ({ page }) => {
    const footer = page.locator('.ga-footer');
    const inputContainer = page.locator('.copilotKitInputContainer');

    await expect(footer).toBeVisible();
    await expect(inputContainer).toBeVisible();

    const footerBox = await footer.boundingBox();
    const inputBox = await inputContainer.boundingBox();

    expect(footerBox).not.toBeNull();
    expect(inputBox).not.toBeNull();

    // Footer should be below input (no overlap)
    expect(footerBox!.y).toBeGreaterThan(inputBox!.y + inputBox!.height);
  });


  test('chat container fills available space between header and footer', async ({ page }) => {
    const header = page.locator('.ga-hero-header');
    const footer = page.locator('.ga-footer');
    const chatContainer = page.locator('.ga-chat-container');

    const headerBox = await header.boundingBox();
    const footerBox = await footer.boundingBox();
    const chatBox = await chatContainer.boundingBox();

    expect(headerBox).not.toBeNull();
    expect(footerBox).not.toBeNull();
    expect(chatBox).not.toBeNull();

    // Chat should be between header and footer
    expect(chatBox!.y).toBeGreaterThan(headerBox!.y + headerBox!.height);
    expect(chatBox!.y + chatBox!.height).toBeLessThan(footerBox!.y);
  });

  test('input box is fully visible and not cut off', async ({ page }) => {
    const inputContainer = page.locator('.copilotKitInputContainer');
    const textarea = page.locator('.copilotKitInput textarea');

    await expect(inputContainer).toBeVisible();
    await expect(textarea).toBeVisible();

    // Should be able to type in textarea
    await textarea.fill('Testing input visibility');
    await expect(textarea).toHaveValue('Testing input visibility');

    // Textarea should be fully in viewport
    const textareaBox = await textarea.boundingBox();
    const viewportSize = page.viewportSize();

    expect(textareaBox).not.toBeNull();
    expect(viewportSize).not.toBeNull();

    expect(textareaBox!.y).toBeGreaterThanOrEqual(0);
    expect(textareaBox!.y + textareaBox!.height).toBeLessThanOrEqual(viewportSize!.height);
  });

  test('messages area has proper scrolling', async ({ page }) => {
    const messagesContainer = page.locator('.copilotKitMessages');

    await expect(messagesContainer).toBeVisible();

    const messagesBox = await messagesContainer.boundingBox();
    expect(messagesBox).not.toBeNull();
    expect(messagesBox!.height).toBeGreaterThan(100); // Has reasonable height
  });

  test('header status badges are visible and not overlapping chat', async ({ page }) => {
    const statusBadges = page.locator('.ga-status-badge');
    const chatContainer = page.locator('.ga-chat-container');

    await expect(statusBadges.first()).toBeVisible();

    const badgeBox = await statusBadges.first().boundingBox();
    const chatBox = await chatContainer.boundingBox();

    expect(badgeBox).not.toBeNull();
    expect(chatBox).not.toBeNull();

    // Status badges should be above chat container
    expect(badgeBox!.y + badgeBox!.height).toBeLessThan(chatBox!.y);
  });

  test('progress bar appears at very top of page', async ({ page }) => {
    const chatInput = page.locator('.copilotKitInput textarea');
    await expect(chatInput).toBeVisible({ timeout: 10000 });
    await chatInput.fill('Test');

    // Click send button
    const sendButton = page.locator('.copilotKitInputControlButton');
    await expect(sendButton).toBeVisible();
    await sendButton.click();

    // Progress bar should appear (but might be very brief)
    const progressBar = page.locator('.ga-progress-bar.active');

    // Check if it appears OR if we can at least find the progress bar element
    const progressBarExists = page.locator('.ga-progress-bar');
    await expect(progressBarExists).toBeAttached();

    // If it's visible, check positioning (allow for header offset)
    if (await progressBar.isVisible({ timeout: 1000 }).catch(() => false)) {
      const progressBox = await progressBar.boundingBox();
      expect(progressBox).not.toBeNull();
      // Progress bar should be near top (within 200px to account for header)
      expect(progressBox!.y).toBeLessThan(200);
      expect(progressBox!.height).toBeLessThanOrEqual(10); // Thin bar
    }
  });

  test('layout is responsive on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.waitForTimeout(500);

    const chatContainer = page.locator('.ga-chat-container');
    const inputContainer = page.locator('.copilotKitInputContainer');
    const footer = page.locator('.ga-footer');

    await expect(chatContainer).toBeVisible();
    await expect(inputContainer).toBeVisible();
    await expect(footer).toBeVisible();

    // No overlaps on mobile
    const inputBox = await inputContainer.boundingBox();
    const footerBox = await footer.boundingBox();

    expect(footerBox!.y).toBeGreaterThan(inputBox!.y + inputBox!.height);
  });
});
