import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  // Authenticated live tests have their own config and explicit credentials.
  testIgnore: ['live-shakedown.spec.ts', 'live-operator.spec.ts'],
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

  use: {
    baseURL: 'http://localhost:3000',
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
    command: 'COGNITO_USER_POOL_ID=us-west-2_example01 COGNITO_CLIENT_ID=exampleclient00000000000000 NEXT_PUBLIC_SKIP_AUTH=true NEXT_PUBLIC_OPERATIONS_UI_ENABLED=true npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
