/**
 * Tests for /api/auth/user endpoint
 *
 * The ID token is CRYPTOGRAPHICALLY VERIFIED (not raw-decoded), so the verifier
 * is mocked: a resolved value = a valid token with those claims, a rejection =
 * a token that fails verification (→ 401).
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/auth/user';

// Mock the JWT verifier. user.ts calls CognitoJwtVerifier.create() at module
// load, so the factory must not reference an outer (not-yet-initialized)
// variable — use an inline mockReturnValue and fetch the verify mock in
// beforeEach (same pattern as the admin-route tests).
jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn().mockReturnValue({ verify: jest.fn() }),
  },
}));

describe('/api/auth/user', () => {
  let mockVerify: jest.Mock;

  beforeEach(async () => {
    jest.clearAllMocks();
    const { CognitoJwtVerifier } = await import('aws-jwt-verify');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockVerify = (CognitoJwtVerifier.create as any)().verify;
  });

  it('should return 405 for non-GET requests', async () => {
    const { req, res } = createMocks({ method: 'POST' });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(405);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Method not allowed' });
  });

  it('should return 401 when no token is present', async () => {
    const { req, res } = createMocks({ method: 'GET', cookies: {} });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData())).toEqual({ error: 'No authentication token found' });
  });

  it('should return verified user info for a valid token', async () => {
    mockVerify.mockResolvedValue({
      email: 'test@example.com',
      sub: 'user-123',
      'cognito:groups': ['admin'],
    });

    const { req, res } = createMocks({
      method: 'GET',
      cookies: { cognito_id_token: 'header.payload.signature' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    expect(JSON.parse(res._getData())).toEqual({
      username: 'test',
      email: 'test@example.com',
      isAdmin: true,
      sub: 'user-123',
    });
  });

  it('should extract username from email and default isAdmin to false', async () => {
    mockVerify.mockResolvedValue({ email: 'john.doe@company.com', sub: 'user-456' });

    const { req, res } = createMocks({
      method: 'GET',
      cookies: { cognito_id_token: 'header.payload.signature' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    expect(data.username).toBe('john.doe');
    expect(data.email).toBe('john.doe@company.com');
    expect(data.isAdmin).toBe(false);
  });

  it('should return 401 (not 500) when the token fails verification', async () => {
    // Forged/expired/malformed token: verify() rejects → auth failure, not a parse error.
    mockVerify.mockRejectedValue(new Error('JwtInvalidSignatureError'));

    const { req, res } = createMocks({
      method: 'GET',
      cookies: { cognito_id_token: 'invalid.token' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Invalid authentication token' });
  });
});
