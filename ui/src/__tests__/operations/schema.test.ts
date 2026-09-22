import fs from 'fs';
import path from 'path';
import {
  OPERATION_STATES,
  OPERATIONS_ROUTES,
  MAX_PAGE_SIZE,
  parseCapabilityDiscovery,
  parseOperationsListResponse,
  parseOperationDetail,
  parseControlResponse,
  SchemaGuardError,
} from '@/operations/schema';

// The frozen, public-safe example fixtures the E4 contract prelude shipped.
// The UI guards MUST accept exactly these and reject anything that carries
// forbidden fields or violates the frozen shape.
const FIXTURE_DIR = path.resolve(
  __dirname,
  '../../../../backend/tests/fixtures/operations/v1',
);

function loadFixture(name: string): unknown {
  return JSON.parse(fs.readFileSync(path.join(FIXTURE_DIR, name), 'utf8'));
}

describe('operations schema guards — frozen contract constants', () => {
  it('freezes the E4 route keys exactly as the contract prelude', () => {
    expect(OPERATIONS_ROUTES).toEqual({
      capabilities: '/operations/capabilities',
      list: '/operations',
      detail: '/operations/{operationId}',
      control: '/operations/control',
      killSwitch: '/operations/control/kill-switch',
    });
  });

  it('caps page size at 50', () => {
    expect(MAX_PAGE_SIZE).toBe(50);
  });

  it('exposes the eleven frozen operation states', () => {
    expect(OPERATION_STATES).toEqual([
      'prepared',
      'pending_approval',
      'approved',
      'dispatched',
      'executing',
      'retry_pending',
      'succeeded',
      'failed',
      'rejected',
      'cancelled',
      'expired',
    ]);
  });
});

describe('parseCapabilityDiscovery', () => {
  it('accepts the frozen valid fixture', () => {
    const doc = loadFixture('operations-capability-discovery.valid.json');
    const parsed = parseCapabilityDiscovery(doc);
    expect(parsed.operations_enabled).toBe(true);
    expect(parsed.capabilities[0].capability_id).toBe('gamelift.capacity-adjustment');
    expect(parsed.capabilities[0].gates.some((g) => g.kind === 'static')).toBe(true);
    expect(parsed.capabilities[0].gates.some((g) => g.kind === 'dynamic')).toBe(true);
  });

  it('rejects a non-object', () => {
    expect(() => parseCapabilityDiscovery(null)).toThrow(SchemaGuardError);
    expect(() => parseCapabilityDiscovery('nope')).toThrow(SchemaGuardError);
  });

  it('rejects a wrong contract_version', () => {
    const doc = loadFixture('operations-capability-discovery.valid.json') as Record<string, unknown>;
    doc.contract_version = '2.0';
    expect(() => parseCapabilityDiscovery(doc)).toThrow(SchemaGuardError);
  });

  it('rejects an unknown gate kind', () => {
    const doc = loadFixture('operations-capability-discovery.valid.json') as {
      capabilities: { gates: { kind: string }[] }[];
    };
    doc.capabilities[0].gates[0].kind = 'sideways';
    expect(() => parseCapabilityDiscovery(doc)).toThrow(SchemaGuardError);
  });

  it('rejects a forbidden identity field anywhere in the payload', () => {
    const doc = loadFixture('operations-capability-discovery.valid.json') as Record<string, unknown>;
    (doc.capabilities as Record<string, unknown>[])[0].account_id = '123456789012';
    expect(() => parseCapabilityDiscovery(doc)).toThrow(/forbidden/i);
  });
});

describe('parseOperationsListResponse', () => {
  it('accepts the frozen valid fixture', () => {
    const doc = loadFixture('operations-list-response.valid.json');
    const parsed = parseOperationsListResponse(doc);
    expect(parsed.operations).toHaveLength(2);
    expect(parsed.operations[0].operation_id).toMatch(/^op_[a-z0-9]{26}$/);
    expect(parsed.next_cursor).toBeTruthy();
  });

  it('rejects a page beyond the max size', () => {
    const doc = loadFixture('operations-list-response.valid.json') as Record<string, unknown>;
    doc.page_size = 51;
    expect(() => parseOperationsListResponse(doc)).toThrow(SchemaGuardError);
  });

  it('rejects an unknown operation state', () => {
    const doc = loadFixture('operations-list-response.valid.json') as {
      operations: { state: string }[];
    };
    doc.operations[0].state = 'imaginary';
    expect(() => parseOperationsListResponse(doc)).toThrow(SchemaGuardError);
  });

  it('rejects an operation summary carrying a fleet id', () => {
    const doc = loadFixture('operations-list-response.valid.json') as {
      operations: Record<string, unknown>[];
    };
    doc.operations[0].fleet_id = 'fleet-abc';
    expect(() => parseOperationsListResponse(doc)).toThrow(/forbidden/i);
  });

  it('rejects an operation id that is not the frozen op_ shape', () => {
    const doc = loadFixture('operations-list-response.valid.json') as {
      operations: { operation_id: string }[];
    };
    doc.operations[0].operation_id = 'not-an-op-id';
    expect(() => parseOperationsListResponse(doc)).toThrow(SchemaGuardError);
  });
});

describe('parseOperationDetail', () => {
  it('accepts the frozen valid fixture and preserves phase ordering', () => {
    const doc = loadFixture('operations-detail-projection.valid.json');
    const parsed = parseOperationDetail(doc);
    expect(parsed.phases.map((p) => p.phase)).toEqual([
      'prepare',
      'approve',
      'dispatch',
      'execute',
      'verify',
      'rollback',
    ]);
    expect(parsed.verification.applicable).toBe(true);
    expect(parsed.rollback.outcome).toBe('not_applicable');
    expect(parsed.evidence.length).toBeGreaterThan(0);
  });

  it('rejects an evidence summary longer than 280 chars', () => {
    const doc = loadFixture('operations-detail-projection.valid.json') as {
      evidence: { summary: string }[];
    };
    doc.evidence[0].summary = 'x'.repeat(281);
    expect(() => parseOperationDetail(doc)).toThrow(SchemaGuardError);
  });

  it('rejects an unknown phase name', () => {
    const doc = loadFixture('operations-detail-projection.valid.json') as {
      phases: { phase: string }[];
    };
    doc.phases[0].phase = 'teleport';
    expect(() => parseOperationDetail(doc)).toThrow(SchemaGuardError);
  });

  it('rejects a raw provider payload smuggled into evidence', () => {
    const doc = loadFixture('operations-detail-projection.valid.json') as {
      evidence: Record<string, unknown>[];
    };
    doc.evidence[0].raw_payload = { secret: true };
    expect(() => parseOperationDetail(doc)).toThrow(/forbidden/i);
  });
});

describe('parseControlResponse', () => {
  it('accepts an applied response with an effective kill-switch', () => {
    const doc = loadFixture('operations-control-response.valid.json');
    const parsed = parseControlResponse(doc);
    expect(parsed.outcome).toBe('applied');
    expect(parsed.effective?.operations_enabled).toBe(true);
    expect(parsed.effective?.capabilities['gamelift.capacity-adjustment'].execute).toBe(true);
  });

  it('accepts a version_conflict response without an effective doc', () => {
    const doc = {
      contract_version: '1.0',
      outcome: 'version_conflict',
      config_version: 9,
      reason_code: 'VERSION_CONFLICT',
    };
    const parsed = parseControlResponse(doc);
    expect(parsed.outcome).toBe('version_conflict');
    expect(parsed.effective).toBeUndefined();
  });

  it('rejects an unknown outcome', () => {
    const doc = loadFixture('operations-control-response.valid.json') as Record<string, unknown>;
    doc.outcome = 'exploded';
    expect(() => parseControlResponse(doc)).toThrow(SchemaGuardError);
  });
});
