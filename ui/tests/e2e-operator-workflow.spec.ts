/**
 * Mocked operator workflow (E4, issue #416).
 *
 * Drives the full operator surface in a real browser against the dev server,
 * with the same-origin `/api/operations/*` proxy routes intercepted and
 * answered with frozen, public-safe fixtures. No live backend or AWS is
 * required — the test exercises the browser -> proxy contract and the operator
 * UX (list -> detail timeline -> kill-switch control with confirmation) end to
 * end. It runs under the local-dev auth bypass the dev server already uses.
 *
 * Tags: @e2e @operator
 */
import { test, expect, type Page } from '@playwright/test';

const CAPABILITIES = {
  contract_version: '1.0',
  generated_at: '2026-01-15T00:01:00Z',
  deployment_mode: 'remediate',
  operations_enabled: true,
  kill_switch_config_version: 7,
  capabilities: [
    {
      capability_id: 'gamelift.capacity-adjustment',
      capability_version: '1.0',
      available: true,
      provisioned: true,
      enabled: true,
      effective_authority: 'remediate',
      phases: { prepare: true, dispatch: true, execute: false },
      gates: [
        { gate_id: 'build.capability-present', kind: 'static', satisfied: true },
        { gate_id: 'runtime.kill-switch-enabled', kind: 'dynamic', satisfied: true },
      ],
    },
  ],
};

const KILL_SWITCH = {
  contract_version: '1.0',
  config_version: 7,
  issued_at: '2026-01-15T00:00:00Z',
  not_after: '2026-01-15T00:05:00Z',
  operations_enabled: true,
  capabilities: {
    'gamelift.capacity-adjustment': { prepare: true, dispatch: true, execute: false },
  },
};

const LIST = {
  contract_version: '1.0',
  page_size: 25,
  operations: [
    {
      operation_id: 'op_00000000000000000000000001',
      capability_id: 'gamelift.capacity-adjustment',
      state: 'pending_approval',
      created_at: '2026-01-15T00:00:10Z',
      updated_at: '2026-01-15T00:00:20Z',
    },
  ],
};

const DETAIL = {
  contract_version: '1.0',
  operation_id: 'op_00000000000000000000000001',
  capability_id: 'gamelift.capacity-adjustment',
  state: 'succeeded',
  created_at: '2026-01-15T00:00:10Z',
  updated_at: '2026-01-15T00:02:00Z',
  phases: [
    { phase: 'prepare', status: 'succeeded', occurred_at: '2026-01-15T00:00:10Z' },
    { phase: 'approve', status: 'succeeded', occurred_at: '2026-01-15T00:00:40Z' },
    { phase: 'verify', status: 'succeeded', occurred_at: '2026-01-15T00:02:00Z' },
  ],
  verification: { applicable: true, outcome: 'succeeded' },
  rollback: { applicable: false, outcome: 'not_applicable' },
  evidence: [
    {
      category: 'authorization',
      summary: 'Authority resolved to remediate; approval required.',
      recorded_at: '2026-01-15T00:00:15Z',
    },
    {
      category: 'approval',
      summary: 'Human approval granted by an authorized admin.',
      recorded_at: '2026-01-15T00:00:40Z',
    },
  ],
};

const CONTROL_RESPONSE = {
  contract_version: '1.0',
  outcome: 'applied',
  config_version: 8,
  reason_code: 'APPLIED',
  effective: { ...KILL_SWITCH, config_version: 8 },
};

async function mockOperationsApi(page: Page): Promise<{ controlBodies: unknown[] }> {
  const controlBodies: unknown[] = [];
  const json = (body: unknown) => ({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });

  await page.route('**/api/operations/capabilities', (route) => route.fulfill(json(CAPABILITIES)));
  await page.route('**/api/operations/kill-switch', (route) => route.fulfill(json(KILL_SWITCH)));
  await page.route(/\/api\/operations\/op_[a-z0-9]{26}$/, (route) => route.fulfill(json(DETAIL)));
  await page.route('**/api/operations/control', (route) => {
    controlBodies.push(JSON.parse(route.request().postData() || '{}'));
    return route.fulfill(json(CONTROL_RESPONSE));
  });
  // The bare list route (with or without a query string), matched last.
  await page.route(/\/api\/operations(\?.*)?$/, (route) => route.fulfill(json(LIST)));

  return { controlBodies };
}

test.describe('Operator workflow (mocked backend)', { tag: ['@e2e', '@operator'] }, () => {
  test('lists operations, opens the detail timeline, and applies a kill-switch change', async ({
    page,
  }) => {
    const { controlBodies } = await mockOperationsApi(page);

    await page.goto('/operations');
    await page.waitForLoadState('domcontentloaded');

    // 1. The list renders the operation summary.
    await expect(page.getByText('op_00000000000000000000000001')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Pending approval')).toBeVisible();

    // 2. Selecting an operation opens the detail timeline distinguishing phases.
    await page.getByRole('button', { name: /op_00000000000000000000000001/i }).click();
    const detail = page.getByRole('region', { name: /operation op_00000000000000000000000001/i });
    const timeline = detail.getByRole('list', { name: /lifecycle timeline/i });
    await expect(timeline.getByText('Proposal')).toBeVisible();
    await expect(timeline.getByText('Approval')).toBeVisible();
    await expect(timeline.getByText('Verification')).toBeVisible();

    // 3. The kill-switch panel shows static and dynamic gates.
    const killSwitch = page.getByRole('region', { name: /kill-switch controls/i });
    await expect(killSwitch.getByText('build.capability-present')).toBeVisible();
    await expect(killSwitch.getByText('static')).toBeVisible();
    await expect(killSwitch.getByText('dynamic')).toBeVisible();

    // 4. Toggle execute on, apply, confirm.
    await killSwitch.getByRole('switch', { name: /gamelift\.capacity-adjustment execute/i }).click();
    await killSwitch.getByRole('button', { name: /apply changes/i }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: /confirm/i }).click();

    // 5. The outcome is announced and the request carried the compare-and-set version.
    await expect(killSwitch.getByRole('status').filter({ hasText: /applied/i })).toBeVisible();
    expect(controlBodies).toHaveLength(1);
    const body = controlBodies[0] as {
      expected_config_version: number;
      desired: { capabilities: Record<string, { execute: boolean }> };
    };
    expect(body.expected_config_version).toBe(7);
    expect(body.desired.capabilities['gamelift.capacity-adjustment'].execute).toBe(true);
  });

  test('cancels a pre-dispatch operation from the detail view and refreshes the timeline', async ({
    page,
  }) => {
    // A cancellable (pending_approval) operation; the detail flips to cancelled
    // after the cancel proxy is called, so the refreshed timeline reflects it.
    const cancellableDetail = {
      ...DETAIL,
      state: 'pending_approval',
      phases: [{ phase: 'prepare', status: 'succeeded', occurred_at: '2026-01-15T00:00:10Z' }],
      verification: { applicable: false, outcome: 'not_applicable' },
      rollback: { applicable: false, outcome: 'not_applicable' },
    };
    const cancelledDetail = { ...cancellableDetail, state: 'cancelled', updated_at: '2026-01-15T00:03:00Z' };

    const json = (body: unknown) => ({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });

    let cancelled = false;
    let cancelCalls = 0;

    await page.route('**/api/operations/capabilities', (route) => route.fulfill(json(CAPABILITIES)));
    await page.route('**/api/operations/kill-switch', (route) => route.fulfill(json(KILL_SWITCH)));
    // The cancel action route must be matched BEFORE the bare detail route.
    await page.route(/\/api\/operations\/op_[a-z0-9]{26}\/cancel$/, (route) => {
      cancelCalls += 1;
      cancelled = true;
      return route.fulfill(json({ operation_id: 'op_00000000000000000000000001', new_state: 'cancelled' }));
    });
    await page.route(/\/api\/operations\/op_[a-z0-9]{26}$/, (route) =>
      route.fulfill(json(cancelled ? cancelledDetail : cancellableDetail)),
    );
    await page.route(/\/api\/operations(\?.*)?$/, (route) => route.fulfill(json(LIST)));

    await page.goto('/operations');
    await page.waitForLoadState('domcontentloaded');

    await page.getByRole('button', { name: /op_00000000000000000000000001/i }).click();
    const detail = page.getByRole('region', { name: /operation op_00000000000000000000000001/i });

    // The cancel control is offered for a cancellable state; there is no expire action.
    const cancelBtn = detail.getByRole('button', { name: /cancel operation/i });
    await expect(cancelBtn).toBeVisible();
    await expect(page.getByRole('button', { name: /expire/i })).toHaveCount(0);

    await cancelBtn.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: /cancel operation/i }).click();

    // After a successful cancel the timeline refreshes to the terminal state.
    await expect(detail.getByText('Cancelled')).toBeVisible({ timeout: 10000 });
    expect(cancelCalls).toBe(1);
    // Once terminal, the cancel control is no longer offered.
    await expect(detail.getByRole('button', { name: /cancel operation/i })).toHaveCount(0);
  });

  test('shows an access-denied message when the proxy returns 403', async ({ page }) => {
    await page.route('**/api/operations/capabilities', (route) =>
      route.fulfill({ status: 403, contentType: 'application/json', body: '{"error":"denied"}' }),
    );
    await page.route('**/api/operations/kill-switch', (route) =>
      route.fulfill({ status: 403, contentType: 'application/json', body: '{"error":"denied"}' }),
    );

    await page.goto('/operations');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/you do not have operator access/i)).toBeVisible({ timeout: 10000 });
  });
});
