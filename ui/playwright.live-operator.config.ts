import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the LIVE operator scaffold against the deployed stack
 * (E4, issue #416). Like playwright.live.config.ts, this does NOT start a local
 * webServer and targets SHAKEDOWN_URL. Local/opt-in only — not run in CI.
 */
export default defineConfig({
  testDir: './tests',
  testMatch: 'live-operator.spec.ts',
  timeout: 240_000,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report-live-operator' }]],
  use: {
    baseURL: process.env.SHAKEDOWN_URL,
    trace: 'on',
    screenshot: 'on',
    video: 'retain-on-failure',
    ...devices['Desktop Chrome'],
    channel: 'chrome',
  },
});
