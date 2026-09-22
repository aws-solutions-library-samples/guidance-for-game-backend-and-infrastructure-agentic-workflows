import Link from 'next/link';
import type { CapabilityDiscovery } from '@/operations/schema';

interface OperatorNavProps {
  isAdmin: boolean;
  capabilities: CapabilityDiscovery[];
}

/**
 * The operator navigation entry. It appears ONLY when:
 *  - the user is an admin, AND
 *  - capability discovery reports at least one capability that is both
 *    available (code path exists) and provisioned (resources created).
 *
 * Visibility is a UX affordance, never authorization: the proxy routes and the
 * backend independently re-check the admin group and every gate. `enabled`
 * (the runtime kill-switch) is intentionally NOT required to show the entry, so
 * an admin can still reach the console to inspect gate state and flip switches.
 */
export default function OperatorNav({ isAdmin, capabilities }: OperatorNavProps) {
  if (!isAdmin) return null;
  const usable = capabilities.some((c) => c.available && c.provisioned);
  if (!usable) return null;

  return (
    <Link href="/operations" className="ga-operator-nav-link">
      Operations
      <style jsx>{`
        .ga-operator-nav-link {
          display: inline-block;
          padding: 8px 12px;
          color: var(--ga-accent);
          text-decoration: none;
          font-size: 14px;
          font-weight: 500;
          border-radius: 6px;
        }
        .ga-operator-nav-link:hover {
          background: var(--ga-control-hover-bg);
        }
        .ga-operator-nav-link:focus-visible {
          outline: 2px solid var(--ga-accent);
          outline-offset: 2px;
        }
      `}</style>
    </Link>
  );
}
