import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import capabilitiesHandler from '@/pages/api/operations/capabilities';
import listHandler from '@/pages/api/operations/index';
import detailHandler from '@/pages/api/operations/[operationId]';
import controlHandler from '@/pages/api/operations/control';
import killSwitchHandler from '@/pages/api/operations/kill-switch';

const ENV = { ...process.env };

// Fixtures the mocked backend returns.
import capabilityFixture from '../../../../../backend/tests/fixtures/operations/v1/operations-capability-discovery.valid.json';
import listFixture from '../../../../../backend/tests/fixtures/operations/v1/operations-list-response.valid.json';
import detailFixture from '../../../../../backend/tests/fixtures/operations/v1/operations-detail-projection.valid.json';
import controlResponseFixture from '../../../../../backend/tests/fixtures/operations/v1/operations-control-response.valid.json';
import killSwitchFixture from '../../../../../backend/tests/fixtures/operations/v1/operations-kill-switch.valid.json';

function mockBackend(status: number, body: unknown) {
  global.fetch = jest.fn(async () =>
    ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    }) as unknown as Response,
  ) as unknown as typeof fetch;
}

beforeEach(() => {
  // Local-dev bypass keeps these unit tests free of Cognito keys while still
  // exercising CSRF and validation. Auth-specific enforcement is covered by the
  // proxyAuth + auth tests.
  process.env = {
    ...ENV,
    NODE_ENV: 'development',
    NEXT_PUBLIC_SKIP_AUTH: 'true',
    GBAW_OPERATIONS_API_BASE_URL: 'http://backend.test',
  };
});
afterEach(() => {
  process.env = { ...ENV };
  jest.restoreAllMocks();
});

function get(query: Record<string, unknown> = {}) {
  return createMocks<NextApiRequest, NextApiResponse>({ method: 'GET', query });
}

describe('GET /api/operations/capabilities', () => {
  it('returns the validated capability discovery projection', async () => {
    mockBackend(200, capabilityFixture);
    const { req, res } = get();
    await capabilitiesHandler(req, res);
    expect(res._getStatusCode()).toBe(200);
    const body = JSON.parse(res._getData());
    expect(body.capabilities[0].capability_id).toBe('gamelift.capacity-adjustment');
  });

  it('rejects a non-GET method', async () => {
    mockBackend(200, capabilityFixture);
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({ method: 'POST' });
    await capabilitiesHandler(req, res);
    expect(res._getStatusCode()).toBe(405);
  });

  it('fails closed (502) when the backend returns a forbidden field', async () => {
    const poisoned = JSON.parse(JSON.stringify(capabilityFixture));
    poisoned.capabilities[0].account_id = '123456789012';
    mockBackend(200, poisoned);
    const { req, res } = get();
    await capabilitiesHandler(req, res);
    expect(res._getStatusCode()).toBe(502);
    expect(res._getData()).not.toContain('123456789012');
  });

  it('propagates a backend auth failure as an upstream error, not a 200', async () => {
    mockBackend(403, { error: 'denied' });
    const { req, res } = get();
    await capabilitiesHandler(req, res);
    expect(res._getStatusCode()).toBe(403);
  });
});

describe('GET /api/operations (list)', () => {
  it('returns the validated list and forwards bounded query params', async () => {
    mockBackend(200, listFixture);
    const { req, res } = get({ page_size: '25', states: 'pending_approval,succeeded' });
    await listHandler(req, res);
    expect(res._getStatusCode()).toBe(200);
    const body = JSON.parse(res._getData());
    expect(body.operations).toHaveLength(2);
    const calledUrl = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(calledUrl).toContain('/operations');
    expect(calledUrl).toContain('page_size=25');
  });

  it('rejects a page_size above the frozen max of 50 (400)', async () => {
    mockBackend(200, listFixture);
    const { req, res } = get({ page_size: '999' });
    await listHandler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects an unknown filter state (400)', async () => {
    mockBackend(200, listFixture);
    const { req, res } = get({ states: 'not_a_state' });
    await listHandler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe('GET /api/operations/[operationId] (detail)', () => {
  it('returns the validated detail projection for a well-formed id', async () => {
    mockBackend(200, detailFixture);
    const { req, res } = get({ operationId: 'op_00000000000000000000000001' });
    await detailHandler(req, res);
    expect(res._getStatusCode()).toBe(200);
    const body = JSON.parse(res._getData());
    expect(body.phases.length).toBeGreaterThan(0);
  });

  it('rejects a malformed operation id without calling the backend (400)', async () => {
    mockBackend(200, detailFixture);
    const { req, res } = get({ operationId: '../etc/passwd' });
    await detailHandler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe('GET /api/operations/kill-switch', () => {
  it('returns the validated kill-switch document', async () => {
    mockBackend(200, killSwitchFixture);
    const { req, res } = get();
    await killSwitchHandler(req, res);
    expect(res._getStatusCode()).toBe(200);
    const body = JSON.parse(res._getData());
    expect(body.capabilities['gamelift.capacity-adjustment']).toBeDefined();
  });
});

describe('POST /api/operations/control', () => {
  const validBody = {
    contract_version: '1.0',
    expected_config_version: 7,
    desired: {
      operations_enabled: true,
      capabilities: {
        'gamelift.capacity-adjustment': { prepare: true, dispatch: true, execute: false },
      },
    },
  };

  function post(body: unknown, headers: Record<string, string> = {}) {
    return createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body,
    });
  }

  it('applies a valid control request and returns the validated response', async () => {
    mockBackend(200, controlResponseFixture);
    const { req, res } = post(validBody);
    await controlHandler(req, res);
    expect(res._getStatusCode()).toBe(200);
    const body = JSON.parse(res._getData());
    expect(body.outcome).toBe('applied');
    // The forwarded body must be exactly the frozen request shape.
    const forwarded = JSON.parse((global.fetch as jest.Mock).mock.calls[0][1].body);
    expect(forwarded).toEqual(validBody);
  });

  it('rejects a non-POST method (405)', async () => {
    mockBackend(200, controlResponseFixture);
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({ method: 'GET' });
    await controlHandler(req, res);
    expect(res._getStatusCode()).toBe(405);
  });

  it('blocks a cross-origin request (403 CSRF) in production', async () => {
    process.env.NODE_ENV = 'production';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    mockBackend(200, controlResponseFixture);
    const { req, res } = post(validBody, { host: 'ui.example.com', origin: 'https://evil.example.com' });
    await controlHandler(req, res);
    expect(res._getStatusCode()).toBe(403);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects a control body carrying identity/policy (400) and never forwards it', async () => {
    mockBackend(200, controlResponseFixture);
    const poisoned = { ...validBody, principal: 'arn:aws:iam::123456789012:user/admin' };
    const { req, res } = post(poisoned);
    await controlHandler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects a malformed control body (400)', async () => {
    mockBackend(200, controlResponseFixture);
    const { req, res } = post({ contract_version: '1.0' });
    await controlHandler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
