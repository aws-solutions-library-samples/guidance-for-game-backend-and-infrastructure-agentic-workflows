/**
 * Live shakedown — authenticated end-to-end test against the DEPLOYED stack.
 *
 * Unlike e2e-smoke (which runs locally with NEXT_PUBLIC_SKIP_AUTH), this logs in
 * through real Cognito and drives real queries through the orchestrator to each
 * specialist agent — exercising the live MCP servers (GameLift/EKS/Cost).
 *
 * Run against the deployed URL with real creds:
 *   SHAKEDOWN_URL=https://...ecs.us-west-2.on.aws \
 *   SHAKEDOWN_EMAIL=... SHAKEDOWN_PASSWORD=... \
 *   npx playwright test tests/live-shakedown.spec.ts --config=playwright.live.config.ts
 *
 * Tags: @live
 */

import { test, expect } from '@playwright/test';
import { assertHealthySpecialistReply, sendAndAwaitReply } from './helpers/live-chat';

const BASE = process.env.SHAKEDOWN_URL!;
const EMAIL = process.env.SHAKEDOWN_EMAIL!;
const PASSWORD = process.env.SHAKEDOWN_PASSWORD!;

// Each query is phrased to route to a specific specialist, which calls its MCP
// server (or boto3 for GameLift). `expects` are lowercase substrings at least
// one of which must appear in the reply — loose enough to survive phrasing
// changes, tight enough to prove the specialist actually answered from live
// AWS data rather than deflecting.
const SPECIALIST_QUERIES = [
  { name: 'GameLift', q: 'List my GameLift fleets and their status.', expects: ['fleet'] },
  { name: 'EKS', q: 'Show me my EKS clusters.', expects: ['cluster'] },
  { name: 'Cost', q: 'What were my AWS costs over the last 7 days?', expects: ['cost', '$', 'spend'] },
];

test.describe('Live shakedown (authenticated, real MCP)', { tag: ['@live'] }, () => {
  test.beforeAll(() => {
    if (!BASE || !EMAIL || !PASSWORD) {
      throw new Error('SHAKEDOWN_URL, SHAKEDOWN_EMAIL, SHAKEDOWN_PASSWORD must be set');
    }
  });

  test('loads and shows login', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    // Client-rendered Next.js app: <title> is applied after React hydration,
    // so poll rather than assert immediately.
    await expect(page).toHaveTitle(/Game Agent/i, { timeout: 20000 });
    // Cognito login form present
    await expect(page.locator('input[type="email"]')).toBeVisible({ timeout: 15000 });
  });

  test('logs in and exercises each specialist (real MCP calls)', async ({ page }) => {
    test.setTimeout(900_000); // three agent + MCP round-trips, each up to ~3 min

    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    // --- Cognito login ---
    await page.locator('input[type="email"]').fill(EMAIL);
    await page.locator('input[type="password"]').fill(PASSWORD);
    // The page has two "Sign In" buttons (tab toggle + form submit); target the
    // form's submit button specifically to avoid a strict-mode multi-match.
    await page.locator('button[type="submit"].auth-submit').click();

    // Chat interface (CopilotKit) should appear post-auth
    await expect(page.locator('textarea').first()).toBeVisible({ timeout: 30000 });

    // --- Drive each specialist query and assert a real, on-topic response ---
    for (const { name, q, expects } of SPECIALIST_QUERIES) {
      const reply = await sendAndAwaitReply(page, q);
      assertHealthySpecialistReply(reply, expects, name);
    }
  });
});
