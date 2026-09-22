import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';

jest.mock('@/operations/client', () => {
  const actual = jest.requireActual('@/operations/client');
  return {
    __esModule: true,
    ...actual,
    fetchCapabilities: jest.fn(),
    fetchKillSwitch: jest.fn(),
    fetchOperations: jest.fn(),
    fetchOperationDetail: jest.fn(),
    submitControl: jest.fn(),
  };
});
import * as client from '@/operations/client';
import OperationsPage from '@/pages/operations';
import { ThemeProvider } from '@/components/ThemeProvider';
import type {
  CapabilityDiscoveryDocument,
  KillSwitchDocument,
  OperationsListResponse,
  OperationDetail,
} from '@/operations/schema';

const capabilities: CapabilityDiscoveryDocument = {
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
      gates: [{ gate_id: 'build.capability-present', kind: 'static', satisfied: true }],
    },
  ],
};

const killSwitch: KillSwitchDocument = {
  contract_version: '1.0',
  config_version: 7,
  issued_at: '2026-01-15T00:00:00Z',
  not_after: '2026-01-15T00:05:00Z',
  operations_enabled: true,
  capabilities: {
    'gamelift.capacity-adjustment': { prepare: true, dispatch: true, execute: false },
  },
};

const list: OperationsListResponse = {
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

const detail: OperationDetail = {
  contract_version: '1.0',
  operation_id: 'op_00000000000000000000000001',
  capability_id: 'gamelift.capacity-adjustment',
  state: 'succeeded',
  created_at: '2026-01-15T00:00:10Z',
  updated_at: '2026-01-15T00:02:00Z',
  phases: [{ phase: 'prepare', status: 'succeeded', occurred_at: '2026-01-15T00:00:10Z' }],
  verification: { applicable: true, outcome: 'succeeded' },
  rollback: { applicable: false, outcome: 'not_applicable' },
  evidence: [{ category: 'approval', summary: 'Approved.', recorded_at: '2026-01-15T00:00:40Z' }],
};

const mocked = client as jest.Mocked<typeof client>;

beforeEach(() => {
  mocked.fetchCapabilities.mockResolvedValue(capabilities);
  mocked.fetchKillSwitch.mockResolvedValue(killSwitch);
  mocked.fetchOperations.mockResolvedValue(list);
  mocked.fetchOperationDetail.mockResolvedValue(detail);
});
afterEach(() => jest.clearAllMocks());

function renderPage() {
  return render(
    <ThemeProvider>
      <OperationsPage />
    </ThemeProvider>,
  );
}

describe('OperationsPage', () => {
  it('loads and renders the operation list and the kill-switch panel', async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('op_00000000000000000000000001')).toBeInTheDocument(),
    );
    expect(screen.getByRole('region', { name: /kill-switch controls/i })).toBeInTheDocument();
  });

  it('opens the detail timeline when an operation is selected', async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('op_00000000000000000000000001')).toBeInTheDocument(),
    );
    await userEvent.click(
      screen.getByRole('button', { name: /op_00000000000000000000000001/i }),
    );
    await waitFor(() => expect(mocked.fetchOperationDetail).toHaveBeenCalled());
    const detailRegion = await screen.findByRole('region', {
      name: /operation op_00000000000000000000000001/i,
    });
    expect(within(detailRegion).getByText(/proposal/i)).toBeInTheDocument();
  });

  it('shows an access-denied message when the user is not an operator (403)', async () => {
    mocked.fetchCapabilities.mockRejectedValue(
      new client.OperationsClientError(403, 'forbidden'),
    );
    renderPage();
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/access/i));
  });

  it('has no detectable accessibility violations once loaded', async () => {
    const { container } = renderPage();
    await waitFor(() =>
      expect(screen.getByText('op_00000000000000000000000001')).toBeInTheDocument(),
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
