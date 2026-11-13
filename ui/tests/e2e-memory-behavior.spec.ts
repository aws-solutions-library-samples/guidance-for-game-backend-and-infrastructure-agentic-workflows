/**
 * Memory System E2E Tests
 *
 * Tests short-term and long-term memory functionality in local environment.
 * These tests verify that the memory system works correctly with real AgentCore Runtime.
 * Tags: @e2e @memory @integration @ai @slow
 */

import { test, expect } from '@playwright/test';
import { sendAndAwaitReply } from './helpers/live-chat';

const LOCAL_URL = 'http://localhost:3000';

// Every recall assertion below needs the local backend running with AgentCore
// Memory. Without a memory resource the backend logs "Memory configured: False"
// and these tests can only fail, so skip them with the setup they need.
test.skip(
  !process.env.BEDROCK_AGENTCORE_MEMORY_ID,
  'Requires AgentCore Memory: start the local backend with BEDROCK_AGENTCORE_MEMORY_ID set, and export the same value for this Playwright run'
);

test.describe('Memory System - Local Environment', { tag: ['@e2e', '@memory', '@integration', '@ai', '@slow'] }, () => {
  // These tests share one local dev actor and its memory, and each one sends
  // several real agent turns. Run them one at a time so parallel workers do not
  // trip the local rate limit or interleave memory writes (#410).
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(300000); // Multiple real agent turns per test

  test.beforeEach(async ({ page }) => {
    // Navigate to local app
    await page.goto(LOCAL_URL);
    await page.waitForSelector('textarea', { timeout: 10000 });
  });

  test('STM: Should remember name within same session', async ({ page }) => {
    console.log('🧪 Testing Short-Term Memory (STM)...');

    // Step 1: Introduce yourself
    console.log('📝 Step 1: Introducing as Test User...');
    const response1 = await sendAndAwaitReply(page, 'Hello! My name is Test User.');
    console.log('✅ Response 1 received');

    // Step 2: Ask for name recall
    console.log('📝 Step 2: Asking for name recall...');
    const response2 = await sendAndAwaitReply(page, "What's my name?");
    console.log('✅ Response 2:', response2.substring(0, 200));

    // Verify name is remembered. Match the distinctive tokens, not the exact
    // phrase, so "Test-User" or "TestUser" in the reply still counts (#410).
    expect(response2).toMatch(/test[\s-]*user/i);
    console.log('✅ PASS: STM working - name remembered in same session');
  });


  test('STM: Should maintain conversation context', async ({ page }) => {
    console.log('🧪 Testing conversation context (STM)...');

    // Step 1: Provide context
    console.log('📝 Step 1: Providing context...');
    await sendAndAwaitReply(page, 'I have 5 GameLift fleets running.');

    // Step 2: Ask follow-up question
    console.log('📝 Step 2: Asking follow-up...');
    const response = await sendAndAwaitReply(page, 'How many fleets did I say I have?');
    console.log('✅ Response:', response.substring(0, 200));

    // Verify context is maintained
    expect(response).toMatch(/5|five/i);
    console.log('✅ PASS: Conversation context maintained');
  });

  test('Memory: Should handle multiple facts', async ({ page }) => {
    console.log('🧪 Testing multiple facts storage...');

    // Provide multiple facts
    console.log('📝 Providing multiple facts...');
    await sendAndAwaitReply(page, 'My name is Multi Fact User and I work on GameLift.');

    // Ask about first fact
    console.log('📝 Asking about name...');
    const response1 = await sendAndAwaitReply(page, "What's my name?");
    // The model may shorten the name ("your name is Multi Fact"), so require
    // only its distinctive tokens rather than the full phrase (#410).
    expect(response1).toMatch(/multi[\s-]*fact/i);

    // Ask about second fact
    console.log('📝 Asking about work...');
    const response2 = await sendAndAwaitReply(page, 'What do I work on?');
    expect(response2).toMatch(/game[\s-]*lift/i);

    console.log('✅ PASS: Multiple facts stored and retrieved');
  });
});
