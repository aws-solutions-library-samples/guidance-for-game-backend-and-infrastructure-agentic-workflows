import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';
import OperatorNav from '@/components/operations/OperatorNav';
import type { CapabilityDiscovery } from '@/operations/schema';

function cap(overrides: Partial<CapabilityDiscovery> = {}): CapabilityDiscovery {
  return {
    capability_id: 'gamelift.capacity-adjustment',
    capability_version: '1.0',
    available: true,
    provisioned: true,
    enabled: true,
    effective_authority: 'remediate',
    phases: { prepare: true, dispatch: true, execute: false },
    gates: [{ gate_id: 'build.capability-present', kind: 'static', satisfied: true }],
    ...overrides,
  };
}

describe('OperatorNav', () => {
  it('renders the operator link when the user is admin and a capability is available/provisioned', () => {
    render(<OperatorNav isAdmin capabilities={[cap()]} />);
    expect(screen.getByRole('link', { name: /operations/i })).toBeInTheDocument();
  });

  it('renders nothing when the user is not an admin', () => {
    const { container } = render(<OperatorNav isAdmin={false} capabilities={[cap()]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when no capability is available', () => {
    const { container } = render(
      <OperatorNav isAdmin capabilities={[cap({ available: false, provisioned: false })]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when capabilities are not provisioned even if available', () => {
    const { container } = render(
      <OperatorNav isAdmin capabilities={[cap({ provisioned: false })]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the discovery list is empty', () => {
    const { container } = render(<OperatorNav isAdmin capabilities={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('has no detectable accessibility violations when shown', async () => {
    const { container } = render(<OperatorNav isAdmin capabilities={[cap()]} />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
