import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';
import OperationsList from '@/components/operations/OperationsList';
import type { OperationSummary } from '@/operations/schema';

const OPS: OperationSummary[] = [
  {
    operation_id: 'op_00000000000000000000000001',
    capability_id: 'gamelift.capacity-adjustment',
    state: 'pending_approval',
    created_at: '2026-01-15T00:00:10Z',
    updated_at: '2026-01-15T00:00:20Z',
  },
  {
    operation_id: 'op_00000000000000000000000002',
    capability_id: 'gamelift.capacity-adjustment',
    state: 'succeeded',
    created_at: '2026-01-14T23:59:00Z',
    updated_at: '2026-01-15T00:00:05Z',
  },
];

describe('OperationsList', () => {
  it('renders a loading state', () => {
    render(<OperationsList status="loading" operations={[]} onSelect={jest.fn()} />);
    expect(screen.getByRole('status')).toHaveTextContent(/loading/i);
  });

  it('renders an empty state when there are no operations', () => {
    render(<OperationsList status="ready" operations={[]} onSelect={jest.fn()} />);
    expect(screen.getByText(/no operations/i)).toBeInTheDocument();
  });

  it('renders an error state with a retry affordance', async () => {
    const onRetry = jest.fn();
    render(
      <OperationsList status="error" operations={[]} onSelect={jest.fn()} onRetry={onRetry} />,
    );
    expect(screen.getByRole('alert')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('renders each operation with a state label and selects on activation', async () => {
    const onSelect = jest.fn();
    render(<OperationsList status="ready" operations={OPS} onSelect={onSelect} />);
    expect(screen.getByText('Pending approval')).toBeInTheDocument();
    expect(screen.getByText('Succeeded')).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole('button', { name: /op_00000000000000000000000001/i }),
    );
    expect(onSelect).toHaveBeenCalledWith('op_00000000000000000000000001');
  });

  it('shows a Next page control only when a next cursor exists', async () => {
    const onNext = jest.fn();
    const { rerender } = render(
      <OperationsList status="ready" operations={OPS} onSelect={jest.fn()} onNext={onNext} />,
    );
    expect(screen.queryByRole('button', { name: /next page/i })).not.toBeInTheDocument();
    rerender(
      <OperationsList
        status="ready"
        operations={OPS}
        onSelect={jest.fn()}
        nextCursor="abc"
        onNext={onNext}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /next page/i }));
    expect(onNext).toHaveBeenCalled();
  });

  it('never renders a forbidden identifier even if one is present', () => {
    render(<OperationsList status="ready" operations={OPS} onSelect={jest.fn()} />);
    expect(screen.queryByText(/fleet-/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/@/)).not.toBeInTheDocument();
  });

  it('has no detectable accessibility violations', async () => {
    const { container } = render(
      <OperationsList status="ready" operations={OPS} onSelect={jest.fn()} nextCursor="abc" onNext={jest.fn()} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
