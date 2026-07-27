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

import { test, expect, Page } from '@playwright/test';

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

const ASSISTANT_MSG = '.copilotKitAssistantMessage';

/**
 * Send a chat message and wait for the NEW assistant reply to finish streaming.
 * Returns the reply text. Counting assistant messages (rather than measuring
 * total innerText growth) is essential: the user's own message echoes into the
 * list immediately, which would satisfy any length-based check before the
 * agent has replied at all.
 */
async function sendAndAwaitReply(page: Page, query: string): Promise<string> {
  const before = await page.locator(ASSISTANT_MSG).count();

  const chatInput = page.locator('textarea').first();
  await chatInput.fill(query);
  await chatInput.press('Enter');

  // A new assistant message appears once the orchestrator responds.
  // Orchestrator -> specialist -> MCP round trips run 20-90s.
  await expect
    .poll(async () => page.locator(ASSISTANT_MSG).count(), {
      timeout: 180_000,
      intervals: [2_000],
    })
    .toBeGreaterThan(before);

  // Wait for streaming to settle: the reply is done when its text is
  // non-trivial and unchanged across two consecutive polls.
  const reply = page.locator(ASSISTANT_MSG).last();
  let prev = '';
  await expect
    .poll(
      async () => {
        const cur = (await reply.innerText()).trim();
        const settled = cur.length > 20 && cur === prev;
        prev = cur;
        return settled;
      },
      { timeout: 120_000, intervals: [3_000] }
    )
    .toBe(true);

  return prev;
}

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
      const lower = reply.toLowerCase();

      // Fail loudly on visible runtime/MCP errors surfaced to the user.
      expect(lower, `${name} reply surfaced an error: ${reply.slice(0, 200)}`).not.toMatch(
        /internal server error|traceback|exception|failed to|unable to reach|mcp .*error|503|502/i
      );

      // The specialist must have answered on-topic, not deflected.
      expect(
        expects.some((s) => lower.includes(s)),
        `${name} reply looks off-topic (expected one of ${JSON.stringify(expects)}): ${reply.slice(0, 200)}`
      ).toBe(true);
    }
  });
});
