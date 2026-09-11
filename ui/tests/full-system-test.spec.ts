/**
 * Full System End-to-End Test
 *
 * Tests complete user flow including:
 * - Login with Cognito
 * - Chat interactions that exercise MCP tools
 * - Memory persistence
 * - All three specialist agents (GameLift, EKS, Cost)
 */

import { test, expect } from '@playwright/test';
import * as path from 'path';
import { assertHealthySpecialistReply, sendAndAwaitReply } from './helpers/live-chat';

// Get credentials from environment
const TEST_EMAIL = process.env.TEST_EMAIL;
const TEST_PASSWORD = process.env.TEST_PASSWORD;
const FRONTEND_URL = process.env.FRONTEND_URL;

// Skip if no credentials or URL provided
test.skip(!TEST_EMAIL || !TEST_PASSWORD || !FRONTEND_URL, 'TEST_EMAIL, TEST_PASSWORD, and FRONTEND_URL environment variables required');

test.describe('Full System Test', () => {

  test('should login, chat, and exercise MCP tools', async ({ page }, testInfo) => {
    test.setTimeout(900_000);
    test.setTimeout(900_000); // Cognito login plus five live agent/MCP round-trips
    // Use Playwright's output directory for screenshots
    const screenshotDir = testInfo.outputDir;
    // Get the deployed frontend URL (required via env var)
    const frontendUrl = FRONTEND_URL!;

    console.log('\n========================================');
    console.log('Full System Test');
    console.log(`URL: ${frontendUrl}`);
    console.log(`User: ${TEST_EMAIL}`);
    console.log('========================================\n');

    // Capture network requests for debugging
    const networkRequests: string[] = [];
    page.on('request', request => {
      const url = request.url();
      if (url.includes('cognito') || url.includes('api') || url.includes('bedrock')) {
        networkRequests.push(`${request.method()} ${url}`);
      }
    });

    page.on('response', response => {
      const url = response.url();
      if (url.includes('cognito') || url.includes('api') || url.includes('bedrock')) {
        console.log(`  Network: ${response.status()} ${url.split('/').pop()}`);
      }
    });

    // Step 1: Navigate to app
    console.log('Step 1: Navigating to app...');
    await page.goto(frontendUrl);
    await page.waitForLoadState('networkidle');
    console.log('  Page loaded');

    // Step 2: Login
    console.log('\nStep 2: Logging in...');

    // Fill email
    const emailInput = page.locator('input[type="email"]').first();
    await expect(emailInput).toBeVisible({ timeout: 10000 });
    await emailInput.fill(TEST_EMAIL!);
    console.log('  Email filled');

    // Fill password
    const passwordInput = page.locator('input[type="password"]').first();
    await expect(passwordInput).toBeVisible();
    await passwordInput.fill(TEST_PASSWORD!);
    console.log('  Password filled');

    // Click submit
    const submitButton = page.locator('button[type="submit"]').first();
    await submitButton.click();
    console.log('  Submit clicked');

    // Wait for chat interface to appear (indicates successful login)
    console.log('  Waiting for chat interface...');
    const chatTextarea = page.locator('textarea').first();
    await expect(chatTextarea).toBeVisible({ timeout: 30000 });
    console.log('  Login successful - chat interface visible');

    // Take screenshot of logged-in state
    await page.screenshot({ path: path.join(screenshotDir, 'system-test-01-logged-in.png'), fullPage: true });

    // Step 3: Test queries that exercise different specialists
    console.log('\n========================================');
    console.log('Step 3: Testing Chat Queries');
    console.log('========================================\n');

    const testQueries = [
      {
        query: 'What can you help me with?',
        description: 'Basic capabilities',
        expectedTerms: ['aws', 'gamelift', 'eks', 'cost'],
      },
      {
        query: 'What are my current AWS costs this month?',
        description: 'Cost Specialist',
        expectedTerms: ['cost', '$', 'spend'],
      },
      {
        query: 'List my EKS clusters',
        description: 'EKS Specialist',
        expectedTerms: ['cluster', 'eks'],
      },
      {
        query: 'Show me GameLift fleet status',
        description: 'GameLift Specialist',
        expectedTerms: ['fleet', 'gamelift'],
      },
      {
        query: 'Do you remember what I asked you about earlier in this conversation?',
        description: 'Conversation memory',
        expectedTerms: ['cost', 'eks', 'gamelift', 'asked'],
      },
    ];

    for (let i = 0; i < testQueries.length; i++) {
      const { query, description, expectedTerms } = testQueries[i];
      console.log(`\nQuery ${i + 1}/${testQueries.length}: ${description}`);

      const reply = await sendAndAwaitReply(page, query);
      assertHealthySpecialistReply(reply, expectedTerms, description);

      await page.screenshot({
        path: path.join(screenshotDir, `system-test-0${i + 2}-query-${i + 1}.png`),
        fullPage: true,
      });
    }

    // Step 4: Verify UI state after all queries
    console.log('\n========================================');
    console.log('Step 4: Final UI State Check');
    console.log('========================================\n');

    // Check we're still logged in
    const userMenu = page.locator('.ga-user-button, [class*="user-menu"], [class*="avatar"]').first();
    const isLoggedIn = await userMenu.isVisible().catch(() => false);
    expect(isLoggedIn).toBe(true);
    console.log(`  User still logged in: ${isLoggedIn}`);

    // Take final screenshot
    await page.screenshot({ path: path.join(screenshotDir, 'system-test-final.png'), fullPage: true });
    console.log('  Final screenshot saved');

    // Print network summary
    console.log('\n========================================');
    console.log('Network Summary');
    console.log('========================================');
    console.log(`Total API/Cognito requests: ${networkRequests.length}`);

    const cognitoReqs = networkRequests.filter(r => r.includes('cognito'));
    const apiReqs = networkRequests.filter(r => r.includes('api'));
    console.log(`  Cognito requests: ${cognitoReqs.length}`);
    console.log(`  API requests: ${apiReqs.length}`);

    console.log('\n========================================');
    console.log('All queries completed');
    console.log('========================================\n');
    console.log('Next: Check CloudWatch logs to verify MCP tools were called');
  });
});
