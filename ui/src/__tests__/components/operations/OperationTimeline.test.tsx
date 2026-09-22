import { render, screen, within, fireEvent, waitFor } from '@testing-library/react';
import { axe } from 'jest-axe';
import fs from 'fs';
import path from 'path';
import OperationTimeline from '@/components/operations/OperationTimeline';
import { parseOperationDetail, type OperationDetail } from '@/operations/schema';

const detail: OperationDetail = parseOperationDetail(
  JSON.parse(
    fs.readFileSync(
      path.resolve(
        __dirname,
        '../../../../../backend/tests/fixtures/operations/v1/operations-detail-projection.valid.json',
      ),
      'utf8',
    ),
  ),
);

/** A cancellable (pre-dispatch) variant derived from the valid fixture. */
const pendingDetail: OperationDetail = { ...detail, state: 'pending_approval' };

describe('OperationTimeline', () => {
  it('renders each lifecycle phase with a distinct label', () => {
    render(<OperationTimeline detail={detail} />);
    const timeline = screen.getByRole('list', { name: /lifecycle timeline/i });
    for (const label of ['Proposal', 'Approval', 'Dispatch', 'Execution', 'Verification', 'Rollback']) {
      expect(within(timeline).getByText(label)).toBeInTheDocument();
    }
  });

  it('distinguishes authorization from approval via evidence', () => {
    render(<OperationTimeline detail={detail} />);
    const evidence = screen.getByRole('list', { name: /evidence/i });
    expect(within(evidence).getByText('Authorization')).toBeInTheDocument();
    expect(within(evidence).getByText('Approval')).toBeInTheDocument();
  });

  it('shows verification and rollback visibility with headings', () => {
    render(<OperationTimeline detail={detail} />);
    const verification = screen.getByRole('heading', { name: 'Verification' });
    const rollback = screen.getByRole('heading', { name: 'Rollback' });
    expect(verification).toBeInTheDocument();
    expect(rollback).toBeInTheDocument();
    // rollback outcome is not_applicable in the fixture.
    const rollbackCard = rollback.parentElement as HTMLElement;
    expect(within(rollbackCard).getByText(/not applicable/i)).toBeInTheDocument();
  });

  it('renders bounded evidence summaries and no raw payload', () => {
    render(<OperationTimeline detail={detail} />);
    expect(
      screen.getByText('Human approval granted by an authorized admin.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/raw_payload|arn:|@/)).not.toBeInTheDocument();
  });

  it('has no detectable accessibility violations', async () => {
    const { container } = render(<OperationTimeline detail={detail} />);
    expect(await axe(container)).toHaveNoViolations();
  });

  describe('cancel control', () => {
    it('does not render a cancel control for a terminal (non-cancellable) state', () => {
      render(<OperationTimeline detail={detail} onCancel={jest.fn()} />);
      expect(screen.queryByRole('button', { name: /cancel operation/i })).not.toBeInTheDocument();
    });

    it('does not render a cancel control when no onCancel handler is provided', () => {
      render(<OperationTimeline detail={pendingDetail} />);
      expect(screen.queryByRole('button', { name: /cancel operation/i })).not.toBeInTheDocument();
    });

    it('never offers an "expire" action (expiry is system-owned)', () => {
      render(<OperationTimeline detail={pendingDetail} onCancel={jest.fn()} />);
      expect(screen.queryByRole('button', { name: /expire/i })).not.toBeInTheDocument();
    });

    it('renders a cancel control for a cancellable state and confirms before calling onCancel', async () => {
      const onCancel = jest.fn().mockResolvedValue(undefined);
      render(<OperationTimeline detail={pendingDetail} onCancel={onCancel} />);

      const trigger = screen.getByRole('button', { name: /cancel operation/i });
      fireEvent.click(trigger);

      // A confirmation dialog appears; onCancel is not called until confirmed.
      const dialog = screen.getByRole('dialog');
      expect(onCancel).not.toHaveBeenCalled();

      const confirm = within(dialog).getByRole('button', { name: /cancel operation|confirm/i });
      fireEvent.click(confirm);

      await waitFor(() => expect(onCancel).toHaveBeenCalledWith(pendingDetail.operation_id));
    });

    it('has no accessibility violations while the confirm dialog is open', async () => {
      const { container } = render(
        <OperationTimeline detail={pendingDetail} onCancel={jest.fn().mockResolvedValue(undefined)} />,
      );
      fireEvent.click(screen.getByRole('button', { name: /cancel operation/i }));
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(await axe(container)).toHaveNoViolations();
    });
  });
});
