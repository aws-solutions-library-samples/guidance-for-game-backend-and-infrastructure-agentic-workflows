import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Navigation from '@/components/Navigation';
import { ThemeProvider } from '@/components/ThemeProvider';

jest.mock('@/operations/client', () => {
  const actual = jest.requireActual('@/operations/client');
  return { __esModule: true, ...actual, fetchCapabilities: jest.fn() };
});
import * as client from '@/operations/client';

const mocked = client as jest.Mocked<typeof client>;

const CAPABILITIES = {
  contract_version: '1.0' as const,
  generated_at: '2026-01-15T00:01:00Z',
  deployment_mode: 'remediate' as const,
  operations_enabled: true,
  capabilities: [
    {
      capability_id: 'gamelift.capacity-adjustment',
      capability_version: '1.0',
      available: true,
      provisioned: true,
      enabled: true,
      effective_authority: 'remediate' as const,
      phases: { prepare: true, dispatch: true, execute: false },
      gates: [{ gate_id: 'build.capability-present', kind: 'static' as const, satisfied: true }],
    },
  ],
};

function mockUser(isAdmin: boolean) {
  global.fetch = jest.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes('/api/auth/user')) {
      return {
        ok: true,
        json: async () => ({ username: 'op', email: 'op@example.com', isAdmin }),
      } as unknown as Response;
    }
    return { ok: true, json: async () => ({}) } as unknown as Response;
  }) as unknown as typeof fetch;
}

function renderNav() {
  return render(
    <ThemeProvider>
      <Navigation pageTitle="Test" />
    </ThemeProvider>,
  );
}

afterEach(() => jest.clearAllMocks());

describe('Navigation operator entry', () => {
  it('shows the Operations entry for an admin when a capability is available/provisioned', async () => {
    mockUser(true);
    mocked.fetchCapabilities.mockResolvedValue(CAPABILITIES);
    renderNav();
    await waitFor(() => expect(mocked.fetchCapabilities).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: /op/i }));
    expect(await screen.findByRole('link', { name: /operations/i })).toBeInTheDocument();
  });

  it('does not show the Operations entry for a non-admin', async () => {
    mockUser(false);
    mocked.fetchCapabilities.mockResolvedValue(CAPABILITIES);
    renderNav();
    await waitFor(() => expect(screen.getByRole('button', { name: /op/i })).toBeInTheDocument());
    expect(mocked.fetchCapabilities).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: /op/i }));
    expect(screen.queryByRole('link', { name: /operations/i })).not.toBeInTheDocument();
  });

  it('does not show the Operations entry when discovery returns no usable capability', async () => {
    mockUser(true);
    mocked.fetchCapabilities.mockResolvedValue({
      ...CAPABILITIES,
      capabilities: [{ ...CAPABILITIES.capabilities[0], available: false, provisioned: false }],
    });
    renderNav();
    await waitFor(() => expect(mocked.fetchCapabilities).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: /op/i }));
    expect(screen.queryByRole('link', { name: /operations/i })).not.toBeInTheDocument();
  });
});
