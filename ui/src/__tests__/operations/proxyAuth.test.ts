import { createMocks } from 'node-mocks-http';
import type { NextApiRequest } from 'next';
import {
  operationsBackendBaseUrl,
  requireOperatorAdmin,
  forwardHeaders,
  OperatorAuthError,
} from '@/operations/proxyAuth';

const ENV = { ...process.env };
afterEach(() => {
  process.env = { ...ENV };
  jest.restoreAllMocks();
});

describe('operationsBackendBaseUrl', () => {
  it('prefers the operations-specific base url', () => {
    process.env.GBAW_OPERATIONS_API_BASE_URL = 'https://ops.example.com/prod';
    process.env.BACKEND_URL = 'http://localhost:8080';
    expect(operationsBackendBaseUrl()).toBe('https://ops.example.com/prod');
  });

  it('falls back to BACKEND_URL then localhost', () => {
    delete process.env.GBAW_OPERATIONS_API_BASE_URL;
    process.env.BACKEND_URL = 'http://localhost:9999';
    expect(operationsBackendBaseUrl()).toBe('http://localhost:9999');
    delete process.env.BACKEND_URL;
    expect(operationsBackendBaseUrl()).toBe('http://localhost:8080');
  });

  it('strips a trailing slash', () => {
    process.env.GBAW_OPERATIONS_API_BASE_URL = 'https://ops.example.com/prod/';
    expect(operationsBackendBaseUrl()).toBe('https://ops.example.com/prod');
  });
});

describe('forwardHeaders', () => {
  it('sends the access token as a Bearer credential and never leaks the id token', () => {
    const headers = forwardHeaders('ACCESS-TOKEN-VALUE');
    expect(headers.Authorization).toBe('Bearer ACCESS-TOKEN-VALUE');
    expect(headers['Content-Type']).toBe('application/json');
    expect(JSON.stringify(headers)).not.toContain('Cookie');
    expect(JSON.stringify(headers)).not.toContain('cognito_id_token');
  });
});

// A stub verifier the helper accepts via dependency injection so we don't need
// real Cognito keys in unit tests.
function stubVerifier(result: Record<string, unknown> | Error) {
  return {
    verify: jest.fn(async () => {
      if (result instanceof Error) throw result;
      return result;
    }),
  };
}

function reqWith(cookies: Record<string, string>): NextApiRequest {
  const cookieHeader = Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join('; ');
  const { req } = createMocks<NextApiRequest>({ headers: { cookie: cookieHeader } });
  return req;
}

describe('requireOperatorAdmin', () => {
  it('returns the verified access token and admin claim when the user is an admin', async () => {
    const req = reqWith({ cognito_access_token: 'AT', cognito_id_token: 'IDT' });
    const result = await requireOperatorAdmin(req, {
      idVerifier: stubVerifier({ sub: 'u1', 'cognito:groups': ['admin', 'users'] }),
      accessVerifier: stubVerifier({ sub: 'u1', token_use: 'access' }),
    });
    expect(result.accessToken).toBe('AT');
    expect(result.isAdmin).toBe(true);
  });

  it('rejects when the id token is missing (401)', async () => {
    const req = reqWith({ cognito_access_token: 'AT' });
    await expect(
      requireOperatorAdmin(req, {
        idVerifier: stubVerifier({ sub: 'u1', 'cognito:groups': ['admin'] }),
        accessVerifier: stubVerifier({ sub: 'u1' }),
      }),
    ).rejects.toMatchObject({ status: 401 });
  });

  it('rejects when the access token is missing (401)', async () => {
    const req = reqWith({ cognito_id_token: 'IDT' });
    await expect(
      requireOperatorAdmin(req, {
        idVerifier: stubVerifier({ sub: 'u1', 'cognito:groups': ['admin'] }),
        accessVerifier: stubVerifier({ sub: 'u1' }),
      }),
    ).rejects.toMatchObject({ status: 401 });
  });

  it('rejects when the user is authenticated but not an admin (403)', async () => {
    const req = reqWith({ cognito_access_token: 'AT', cognito_id_token: 'IDT' });
    await expect(
      requireOperatorAdmin(req, {
        idVerifier: stubVerifier({ sub: 'u1', 'cognito:groups': ['users'] }),
        accessVerifier: stubVerifier({ sub: 'u1', token_use: 'access' }),
      }),
    ).rejects.toMatchObject({ status: 403 });
  });

  it('rejects when the id token fails verification (401)', async () => {
    const req = reqWith({ cognito_access_token: 'AT', cognito_id_token: 'IDT' });
    await expect(
      requireOperatorAdmin(req, {
        idVerifier: stubVerifier(new Error('bad signature')),
        accessVerifier: stubVerifier({ sub: 'u1', token_use: 'access' }),
      }),
    ).rejects.toMatchObject({ status: 401 });
  });

  it('rejects when the access token fails verification (401)', async () => {
    const req = reqWith({ cognito_access_token: 'AT', cognito_id_token: 'IDT' });
    await expect(
      requireOperatorAdmin(req, {
        idVerifier: stubVerifier({ sub: 'u1', 'cognito:groups': ['admin'] }),
        accessVerifier: stubVerifier(new Error('expired')),
      }),
    ).rejects.toMatchObject({ status: 401 });
  });

  it('rejects when the access and id tokens identify different subjects (401)', async () => {
    const req = reqWith({ cognito_access_token: 'AT', cognito_id_token: 'IDT' });
    await expect(
      requireOperatorAdmin(req, {
        idVerifier: stubVerifier({ sub: 'u1', 'cognito:groups': ['admin'] }),
        accessVerifier: stubVerifier({ sub: 'DIFFERENT', token_use: 'access' }),
      }),
    ).rejects.toMatchObject({ status: 401 });
  });

  it('is an OperatorAuthError instance carrying a safe public message', async () => {
    const req = reqWith({});
    const err = await requireOperatorAdmin(req, {
      idVerifier: stubVerifier({ sub: 'u1' }),
      accessVerifier: stubVerifier({ sub: 'u1' }),
    }).catch((e) => e);
    expect(err).toBeInstanceOf(OperatorAuthError);
    expect(err.publicMessage).toBeTruthy();
    // Never echoes token material.
    expect(err.publicMessage).not.toContain('IDT');
    expect(err.publicMessage).not.toContain('AT');
  });
});
