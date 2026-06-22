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

  // REGRESSION GUARD: when CopilotKit was silently bumped 1.10→1.61, the client
  // changed its wire protocol and our proxy swallowed every message — and NO
  // test caught it because nothing exercised the real browser→proxy path. This
  // test sends a message and asserts the CopilotKit client POSTs to the proxy in
  // the GraphQL shape the proxy understands (operationName=generateCopilot
  // response). It needs no live backend — it fails at the protocol layer, which
  // is exactly where the bug lived. If a future CopilotKit bump changes the wire
  // format, this goes red instead of users silently losing messages.
  test('sends a chat message to the proxy in the expected wire protocol', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // The CopilotKit client fires several POSTs to the proxy (availableAgents +
    // loadAgentState on load, then generateCopilotResponse on send). Wait
    // specifically for the SEND request — the one that carries the user message.
    const sendRequest = page.waitForRequest(
      (req) => {
        if (!req.url().includes('/api/copilot/chat') || req.method() !== 'POST') return false;
        try { return JSON.parse(req.postData() || '{}').operationName === 'generateCopilotResponse'; }
        catch { return false; }
      },
      { timeout: 15000 }
    );

    const chatInput = page.locator('textarea, input[type="text"]').first();
    await expect(chatInput).toBeVisible({ timeout: 10000 });
    await chatInput.fill('List my GameLift fleets');
    await chatInput.press('Enter');

    const req = await sendRequest;
    const body = JSON.parse(req.postData() || '{}');

    // The proxy (pages/api/copilot/chat.ts) dispatches on operationName. A 1.50+
    // AG-UI client would send {method,params,body} with NO operationName, which
    // is the silent-swallow failure mode we are guarding against.
    expect(body.operationName).toBe('generateCopilotResponse');
    expect(body.variables?.data?.messages?.length).toBeGreaterThan(0);
  });
});
