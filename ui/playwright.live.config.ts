import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the LIVE shakedown against the deployed stack.
 * Unlike the default config, this does NOT start a local webServer and
 * targets the deployed URL via SHAKEDOWN_URL. Local/dev-only — not run in CI.
 */
export default defineConfig({
  testDir: './tests',
  testMatch: 'live-shakedown.spec.ts',
  timeout: 240_000,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report-live' }]],
  use: {
    baseURL: process.env.SHAKEDOWN_URL,
    trace: 'on',
    screenshot: 'on',
    video: 'retain-on-failure',
    ...devices['Desktop Chrome'],
    // Use the system-installed Chrome (channel) to avoid downloading the
    // bundled chromium build, which is unavailable in some local environments.
    channel: 'chrome',
  },
});
