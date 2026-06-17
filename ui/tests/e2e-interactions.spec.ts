/**
 * Optimized UI Interaction Tests - Fast execution
 * Tags: @e2e @integration @ai @slow
 */

import { test, expect } from '@playwright/test';

test.describe('Game Agent - UI Interaction Tests', { tag: ['@e2e', '@integration', '@ai', '@slow'] }, () => {

  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded'); // Faster than networkidle

    // Wait for chat to be ready - reduced timeout
    await page.waitForSelector('.copilotKitInput textarea', { timeout: 10000 });
  });

  test('should send message and receive AI response', async ({ page }) => {
    const chatInput = page.locator('.copilotKitInput textarea');
    const sendButton = page.locator('.copilotKitInputControlButton');

    await chatInput.fill('Hello');
    await sendButton.click();

    // Verify user message appears
    await expect(page.locator('.copilotKitUserMessage').last()).toContainText('Hello');

    // Wait for AI response - reduced timeout
    await expect(page.locator('.copilotKitAssistantMessage').last()).toBeVisible({ timeout: 30000 });

    // Quick validation
    const response = await page.locator('.copilotKitAssistantMessage').last().textContent();
    expect(response!.length).toBeGreaterThan(10);
  });

  test('should handle AWS service queries', async ({ page }) => {
    const chatInput = page.locator('.copilotKitInput textarea');
    const sendButton = page.locator('.copilotKitInputControlButton');

    await chatInput.fill('List EKS clusters');
    await sendButton.click();

    // Wait for response - reduced timeout
    await expect(page.locator('.copilotKitAssistantMessage').last()).toBeVisible({ timeout: 30000 });

    const response = await page.locator('.copilotKitAssistantMessage').last().textContent();
    expect(response?.toLowerCase()).toMatch(/eks|cluster/);
  });

  test('should handle long messages', async ({ page }) => {
    const chatInput = page.locator('.copilotKitInput textarea');
    const sendButton = page.locator('.copilotKitInputControlButton');

    const longMessage = 'Long message test. '.repeat(20); // Shorter test

    await chatInput.fill(longMessage);

    // Quick height check
    const textareaHeight = await chatInput.evaluate(el => el.scrollHeight);
    expect(textareaHeight).toBeGreaterThan(48);

    await sendButton.click();
    await expect(page.locator('.copilotKitAssistantMessage').last()).toBeVisible({ timeout: 30000 });
  });

  test('should validate empty messages', async ({ page }) => {
    const chatInput = page.locator('.copilotKitInput textarea');
    const sendButton = page.locator('.copilotKitInputControlButton');

    // Quick validation tests
    await chatInput.fill('');
    await expect(sendButton).toBeDisabled();

    await chatInput.fill('hello');
    await expect(sendButton).toBeEnabled();
  });

});
