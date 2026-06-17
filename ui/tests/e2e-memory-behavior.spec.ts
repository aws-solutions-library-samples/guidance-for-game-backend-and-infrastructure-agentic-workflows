/**
 * Memory System E2E Tests
 *
 * Tests short-term and long-term memory functionality in local environment.
 * These tests verify that the memory system works correctly with real AgentCore Runtime.
 * Tags: @e2e @memory @integration @ai @slow
 */

import { test, expect, Page } from '@playwright/test';

const LOCAL_URL = 'http://localhost:3000';

// Helper: Wait for AI response by detecting new content
async function waitForAIResponse(page: Page, timeoutMs: number = 30000): Promise<string> {
  const startTime = Date.now();
  let previousText = await page.locator('body').textContent() || '';

  // Wait for new content to appear (AI response)
  while (Date.now() - startTime < timeoutMs) {
    await page.waitForTimeout(500); // Check every 500ms
    const currentText = await page.locator('body').textContent() || '';

    // If content changed significantly, AI responded
    if (currentText.length > previousText.length + 50) {
      // Wait a bit more for complete response
      await page.waitForTimeout(1000);
      return await page.locator('body').textContent() || '';
    }

    previousText = currentText;
  }

  // Timeout - return what we have
  return previousText;
}

// Helper: Send message in chat
async function sendChatMessage(page: Page, message: string): Promise<void> {
  const textarea = await page.locator('textarea').first();
  await textarea.fill(message);
  await textarea.press('Enter');
}

test.describe('Memory System - Local Environment', { tag: ['@e2e', '@memory', '@integration', '@ai', '@slow'] }, () => {
  test.setTimeout(120000); // 2 minutes per test

  test.beforeEach(async ({ page }) => {
    // Navigate to local app
    await page.goto(LOCAL_URL);
    await page.waitForSelector('textarea', { timeout: 10000 });
  });

  test('STM: Should remember name within same session', async ({ page }) => {
    console.log('🧪 Testing Short-Term Memory (STM)...');

    // Step 1: Introduce yourself
    console.log('📝 Step 1: Introducing as Test User...');
    await sendChatMessage(page, 'Hello! My name is Test User.');
    const response1 = await waitForAIResponse(page, 15000);
    console.log('✅ Response 1 received');

    // Step 2: Ask for name recall
    console.log('📝 Step 2: Asking for name recall...');
    await sendChatMessage(page, "What's my name?");
    const response2 = await waitForAIResponse(page, 15000);
    console.log('✅ Response 2:', response2.substring(0, 200));

    // Verify name is remembered
    expect(response2.toLowerCase()).toContain('test user');
    console.log('✅ PASS: STM working - name remembered in same session');
  });


  test('STM: Should maintain conversation context', async ({ page }) => {
    console.log('🧪 Testing conversation context (STM)...');

    // Step 1: Provide context
    console.log('📝 Step 1: Providing context...');
    await sendChatMessage(page, 'I have 5 GameLift fleets running.');
    await waitForAIResponse(page, 15000);

    // Step 2: Ask follow-up question
    console.log('📝 Step 2: Asking follow-up...');
    await sendChatMessage(page, 'How many fleets did I say I have?');
    const response = await waitForAIResponse(page, 15000);
    console.log('✅ Response:', response.substring(0, 200));

    // Verify context is maintained
    expect(response).toMatch(/5|five/i);
    console.log('✅ PASS: Conversation context maintained');
  });

  test('Memory: Should handle multiple facts', async ({ page }) => {
    console.log('🧪 Testing multiple facts storage...');

    // Provide multiple facts
    console.log('📝 Providing multiple facts...');
    await sendChatMessage(page, 'My name is Multi Fact User and I work on GameLift.');
    await waitForAIResponse(page, 15000);

    // Ask about first fact
    console.log('📝 Asking about name...');
    await sendChatMessage(page, "What's my name?");
    const response1 = await waitForAIResponse(page, 15000);
    expect(response1.toLowerCase()).toContain('multi fact user');

    // Ask about second fact
    console.log('📝 Asking about work...');
    await sendChatMessage(page, 'What do I work on?');
    const response2 = await waitForAIResponse(page, 15000);
    expect(response2.toLowerCase()).toContain('gamelift');

    console.log('✅ PASS: Multiple facts stored and retrieved');
  });
});
