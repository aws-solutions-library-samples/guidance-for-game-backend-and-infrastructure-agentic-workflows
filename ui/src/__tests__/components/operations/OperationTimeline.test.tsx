import { render, screen, within } from '@testing-library/react';
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
});
