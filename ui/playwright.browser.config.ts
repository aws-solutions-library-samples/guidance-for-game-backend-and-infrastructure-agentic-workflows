import { defineConfig, devices } from '@playwright/test';

/**
 * Headless browser regressions for idle-session interaction blockers (#310)
 * that jsdom cannot faithfully reproduce:
 *
 *  - A real, focused CopilotKit-style textarea must NOT submit on Enter once
 *    logout begins (Blocker 3).
 *  - Modal focus must stay trapped while a refresh disables both dialog actions
 *    (Blocker 4).
 *
 * These use `page.setContent()` fixtures and run the SHIPPED guard/trap logic
 * in a real Chromium, so they need no Next.js dev server. That keeps them fast
 * and hermetic while still exercising true browser focus/keydown/submit
 * semantics.
 */
export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list']],
  timeout: 15000,
  expect: { timeout: 5000 },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // No webServer: these fixtures inject their own DOM via setContent.
});
