/**
 * Tests for the E2 operations-action cancel proxy route (#416).
 *
 * Cancellation lives on the E2 operations *action* API (POST
 * /operations/{operationId}/cancel), not the E4 control plane, so it has its
 * own admin+CSRF-guarded proxy route pointed at GBAW_OPERATIONS_ACTION_API_BASE_URL.
 *
 * Coverage: method gate, CSRF (same-origin) enforcement on this state-changing
 * route, operation-id validation, forwarding to the E2 action base (not the
 * control base), a validated public-safe confirmation on success, upstream
 * state-conflict surfaced as a client-safe status, and no credential/identity
 * body being forwarded.
 */
import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import cancelHandler from '@/pages/api/operations/[operationId]/cancel';

const ENV = { ...process.env };

function mockBackend(status: number, body: unknown, headers: Record<string, string> = {}) {
  const encoded = new TextEncoder().encode(JSON.stringify(body));
  const hdr = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  global.fetch = jest.fn(async () => {
    let sent = false;
    return {
      ok: status >= 200 && status < 300,
      status,
      redirected: false,
      headers: { get: (name: string) => hdr.get(name.toLowerCase()) ?? null },
      body: {
        getReader() {
          return {
            read: async () => {
              if (sent) return { done: true, value: undefined };
              sent = true;
              return { done: false, value: encoded };
            },
            cancel: async () => undefined,
            releaseLock: () => undefined,
          };
        },
      },
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
}

const OP_ID = 'op_00000000000000000000000001';

const cancelledStateChange = {
  state_contract_version: '1.0',
  state_change_id: 'state-change:01HZY3N6VQ7X8Y9Z0A1B2C3D4E',
  operation_id: OP_ID,
  prepared_operation_hash:
    'sha256:aed919c38939443654381eb251d9a3bed0112432970d689fbc4683882205e564',
  previous_state: 'pending_approval',
  new_state: 'cancelled',
  attempt: 0,
  reason_code: 'CANCELLED',
  changed_at: '2026-08-13T20:00:02Z',
  actor: { actor_type: 'user', actor_id: 'operator:u1' },
  correlation: { correlation_id: 'correlation:x', request_id: 'request:x' },
};

beforeEach(() => {
  process.env = {
    ...ENV,
    NODE_ENV: 'development',
    NEXT_PUBLIC_SKIP_AUTH: 'true',
    GBAW_OPERATIONS_ACTION_API_BASE_URL: 'http://actions.test',
    GBAW_OPERATIONS_API_BASE_URL: 'http://control.test',
  };
});
afterEach(() => {
  process.env = { ...ENV };
  jest.restoreAllMocks();
});

function cancel(operationId: string, headers: Record<string, string> = {}) {
  return createMocks<NextApiRequest, NextApiResponse>({
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    query: { operationId },
  });
}

it('cancels a cancellable operation and returns a validated confirmation', async () => {
  mockBackend(200, cancelledStateChange);
  const { req, res } = cancel(OP_ID);
  await cancelHandler(req, res);
  expect(res._getStatusCode()).toBe(200);
  const body = JSON.parse(res._getData());
  expect(body).toEqual({ operation_id: OP_ID, new_state: 'cancelled' });
  // Internal ledger detail must not leak to the browser.
  expect(res._getData()).not.toContain('prepared_operation_hash');
  expect(res._getData()).not.toContain('correlation');
});

it('forwards to the E2 action base URL, not the E4 control base', async () => {
  mockBackend(200, cancelledStateChange);
  const { req, res } = cancel(OP_ID);
  await cancelHandler(req, res);
  const calledUrl = (global.fetch as jest.Mock).mock.calls[0][0] as string;
  expect(calledUrl).toContain('actions.test');
  expect(calledUrl).not.toContain('control.test');
  expect(calledUrl).toContain(`/operations/${OP_ID}/cancel`);
});

it('rejects a non-POST method (405)', async () => {
  mockBackend(200, cancelledStateChange);
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
    method: 'GET',
    query: { operationId: OP_ID },
  });
  await cancelHandler(req, res);
  expect(res._getStatusCode()).toBe(405);
});

it('rejects a malformed operation id without calling the backend (400)', async () => {
  mockBackend(200, cancelledStateChange);
  const { req, res } = cancel('../etc/passwd');
  await cancelHandler(req, res);
  expect(res._getStatusCode()).toBe(400);
  expect(global.fetch).not.toHaveBeenCalled();
});

it('blocks a cross-origin request (403 CSRF) in production', async () => {
  process.env.NODE_ENV = 'production';
  process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
  process.env.GBAW_OPERATIONS_ACTION_API_BASE_URL = 'https://actions.example.com';
  mockBackend(200, cancelledStateChange);
  const { req, res } = cancel(OP_ID, {
    host: 'ui.example.com',
    origin: 'https://evil.example.com',
  });
  await cancelHandler(req, res);
  expect(res._getStatusCode()).toBe(403);
  expect(global.fetch).not.toHaveBeenCalled();
});

it('surfaces an upstream state conflict (409) as a client-safe status', async () => {
  mockBackend(409, { error: 'STATE_CONFLICT' });
  const { req, res } = cancel(OP_ID);
  await cancelHandler(req, res);
  expect(res._getStatusCode()).toBe(409);
  expect(res._getData()).not.toContain('STATE_CONFLICT');
});

it('fails closed (502) when the upstream reports a non-cancelled terminal state', async () => {
  mockBackend(200, { ...cancelledStateChange, new_state: 'expired' });
  const { req, res } = cancel(OP_ID);
  await cancelHandler(req, res);
  expect(res._getStatusCode()).toBe(502);
});

it('does not forward any request body to the E2 action API', async () => {
  mockBackend(200, cancelledStateChange);
  const { req, res } = cancel(OP_ID);
  await cancelHandler(req, res);
  const init = (global.fetch as jest.Mock).mock.calls[0][1] as RequestInit;
  // The operation id is a path parameter; there is no identity/credential body.
  expect(init.body === undefined || init.body === 'null' || init.body === '{}').toBe(true);
});
