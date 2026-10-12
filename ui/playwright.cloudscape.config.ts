import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the Cloudscape shell.
 *
 * It serves the app with the server-only shell selector set
 * (`GBAW_UI_SHELL=cloudscape`), so `/` is rewritten to the Cloudscape shell at
 * the same URL, and with `NEXT_PUBLIC_SKIP_AUTH=true` so the authenticated,
 * empty layout renders without a live Cognito backend — the same skip-auth path
 * the current-shell local e2e config uses. The default `playwright.config.ts`
 * ignores `*.cloudscape.spec.ts`, so the current-shell suite is unaffected.
 *
 * The authenticated-journey checks (real sign-in, token refresh, idle warning)
 * against a deployed Cognito backend are validated separately; see the ADR's
 * "Harness auth" open risk.
 */
export default defineConfig({
  testDir: './tests',
  testMatch: ['**/*.cloudscape.spec.ts'],
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['list']],
  timeout: 30000,
  expect: { timeout: 10000 },

  use: {
    baseURL: 'http://localhost:3100',
    trace: 'on-first-retry',
    actionTimeout: 10000,
    navigationTimeout: 15000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    // Dev server with the shell selector on and skip-auth enabled, matching the
    // current-shell local e2e harness. The proxy rewrite runs in development as
    // it does in production.
    command: 'npx next dev --port 3100',
    url: 'http://localhost:3100',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
    env: {
      GBAW_UI_SHELL: 'cloudscape',
      NEXT_PUBLIC_SKIP_AUTH: 'true',
      NODE_ENV: 'development',
    },
  },
});
