import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';
import KillSwitchPanel from '@/components/operations/KillSwitchPanel';
import type { CapabilityDiscovery, KillSwitchDocument } from '@/operations/schema';

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

const capability: CapabilityDiscovery = {
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
};

describe('KillSwitchPanel', () => {
  it('shows the deployment master switch and per-capability phase toggles', () => {
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={jest.fn()} />,
    );
    expect(screen.getByRole('switch', { name: /operations enabled/i })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /prepare/i })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /dispatch/i })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /execute/i })).toBeInTheDocument();
  });

  it('shows static and dynamic gates labelled by kind', () => {
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={jest.fn()} />,
    );
    const gates = screen.getByRole('list', { name: /gates/i });
    expect(within(gates).getByText(/build.capability-present/)).toBeInTheDocument();
    expect(within(gates).getByText(/static/i)).toBeInTheDocument();
    expect(within(gates).getByText(/dynamic/i)).toBeInTheDocument();
  });

  it('requires confirmation before submitting and sends the expected config version', async () => {
    const onSubmit = jest.fn().mockResolvedValue({
      contract_version: '1.0',
      outcome: 'applied',
      config_version: 8,
      reason_code: 'APPLIED',
    });
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={onSubmit} />,
    );
    // Change execute to true then apply.
    await userEvent.click(screen.getByRole('switch', { name: /execute/i }));
    await userEvent.click(screen.getByRole('button', { name: /apply changes/i }));

    // A confirmation dialog appears and holds focus.
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole('button', { name: /confirm/i }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const request = onSubmit.mock.calls[0][0];
    expect(request.expected_config_version).toBe(7);
    expect(request.desired.capabilities['gamelift.capacity-adjustment'].execute).toBe(true);
  });

  it('can cancel the confirmation without submitting', async () => {
    const onSubmit = jest.fn();
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={onSubmit} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply changes/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('announces a successful outcome in a live region', async () => {
    const onSubmit = jest.fn().mockResolvedValue({
      contract_version: '1.0',
      outcome: 'applied',
      config_version: 8,
      reason_code: 'APPLIED',
    });
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={onSubmit} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply changes/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /confirm/i }));
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/applied/i);
    });
  });

  it('announces an error outcome in an alert region', async () => {
    const onSubmit = jest.fn().mockRejectedValue(new Error('boom'));
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={onSubmit} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply changes/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /confirm/i }));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
  });

  it('disables controls while a submission is in flight (loading)', async () => {
    let resolve!: (v: unknown) => void;
    const onSubmit = jest.fn().mockImplementation(() => new Promise((r) => (resolve = r)));
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={onSubmit} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply changes/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /confirm/i }));
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: /operations enabled/i })).toBeDisabled();
    });
    resolve({ contract_version: '1.0', outcome: 'applied', config_version: 8, reason_code: 'APPLIED' });
  });

  it('never renders identity or provider payload from the documents', () => {
    render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={jest.fn()} />,
    );
    expect(screen.queryByText(/@|arn:|fleet-|account/i)).not.toBeInTheDocument();
  });

  it('has no detectable accessibility violations', async () => {
    const { container } = render(
      <KillSwitchPanel killSwitch={killSwitch} capabilities={[capability]} onSubmit={jest.fn()} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
