/**
 * Live operator scaffold (E4, issue #416) — authenticated, against the DEPLOYED
 * stack. Like live-shakedown, this does NOT run in CI and is skipped unless the
 * required environment is present. It logs in through real Cognito as an admin
 * and drives the real operator surface end to end against the deployed E4
 * control plane.
 *
 * This is intentionally a scaffold: it verifies the operator surface loads and
 * the read paths (capabilities/list/detail) work for a real admin. It does NOT
 * flip a live kill switch by default — mutating a deployment's operations state
 * requires an explicit opt-in (LIVE_OPERATOR_ALLOW_CONTROL=true) so the live
 * test never changes production posture as a side effect.
 *
 * Run:
 *   SHAKEDOWN_URL=https://... \
 *   SHAKEDOWN_ADMIN_EMAIL=... SHAKEDOWN_ADMIN_PASSWORD=... \
 *   npx playwright test tests/live-operator.spec.ts --config=playwright.live-operator.config.ts
 *
 * Tags: @live @operator
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.SHAKEDOWN_URL;
const EMAIL = process.env.SHAKEDOWN_ADMIN_EMAIL;
const PASSWORD = process.env.SHAKEDOWN_ADMIN_PASSWORD;
const ALLOW_CONTROL = process.env.LIVE_OPERATOR_ALLOW_CONTROL === 'true';

const CONFIGURED = Boolean(BASE && EMAIL && PASSWORD);

test.describe('Live operator (authenticated, real E4 control plane)', {
  tag: ['@live', '@operator'],
}, () => {
  test.skip(!CONFIGURED, 'Set SHAKEDOWN_URL, SHAKEDOWN_ADMIN_EMAIL, SHAKEDOWN_ADMIN_PASSWORD to run');

  test('signs in as admin and loads the operator console read paths', async ({ page }) => {
    await page.goto(BASE!);
    await page.waitForLoadState('networkidle');

    // Cognito login form (fields are labelled by the app's CognitoAuth component).
    await page.getByLabel(/email/i).fill(EMAIL!);
    await page.getByLabel(/password/i).fill(PASSWORD!);
    await page.getByRole('button', { name: /sign in|log in/i }).click();
    await page.waitForLoadState('networkidle');

    // Navigate to the operator console.
    await page.goto(`${BASE!.replace(/\/$/, '')}/operations`);
    await page.waitForLoadState('networkidle');

    // The console must NOT show the access-denied alert for a real admin.
    await expect(page.getByRole('alert')).toHaveCount(0);

    // The kill-switch region loads (read path via the proxy to the live API).
    await expect(page.getByRole('region', { name: /kill-switch controls/i })).toBeVisible({
      timeout: 20000,
    });
  });

  test('optionally applies a no-op kill-switch change (opt-in only)', async ({ page }) => {
    test.skip(!ALLOW_CONTROL, 'Set LIVE_OPERATOR_ALLOW_CONTROL=true to exercise a live control write');

    await page.goto(BASE!);
    await page.waitForLoadState('networkidle');
    await page.getByLabel(/email/i).fill(EMAIL!);
    await page.getByLabel(/password/i).fill(PASSWORD!);
    await page.getByRole('button', { name: /sign in|log in/i }).click();
    await page.waitForLoadState('networkidle');

    await page.goto(`${BASE!.replace(/\/$/, '')}/operations`);
    await page.waitForLoadState('networkidle');

    const killSwitch = page.getByRole('region', { name: /kill-switch controls/i });
    await expect(killSwitch).toBeVisible({ timeout: 20000 });

    // Apply with no toggle change (a compare-and-set to the same desired state)
    // and confirm; assert the outcome is announced. This is a deliberate no-op
    // that still exercises the write path and the compare-and-set version.
    await killSwitch.getByRole('button', { name: /apply changes/i }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: /confirm/i }).click();
    await expect(page.getByRole('status').filter({ hasText: /applied|conflict|denied/i })).toBeVisible({
      timeout: 20000,
    });
  });
});
