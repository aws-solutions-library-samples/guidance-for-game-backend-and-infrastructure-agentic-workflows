import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  // Authenticated live tests have their own config and explicit credentials.
  // Cloudscape-shell specs run under playwright.cloudscape.config.ts with the
  // shell selector set, so they are excluded here.
  testIgnore: ['live-shakedown.spec.ts', '**/*.cloudscape.spec.ts'],
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['html', { open: 'never' }]],

  // Optimize timeouts for faster tests
  timeout: 15000,
  expect: {
    timeout: 5000,
  },

  // `npm run dev` binds 127.0.0.1 only (#740), so address it directly rather
  // than through `localhost`, which can resolve to ::1 first.
  use: {
    baseURL: 'http://127.0.0.1:3000',
    trace: 'on-first-retry',
    actionTimeout: 10000,
    navigationTimeout: 10000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    command: 'npm run dev',
    url: 'http://127.0.0.1:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
