/**
 * Tests for approve.ts API endpoint - Error handling
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/admin/approve';

// Mock AWS SDK
jest.mock('@aws-sdk/client-cognito-identity-provider', () => ({
  CognitoIdentityProviderClient: jest.fn().mockImplementation(() => ({
    send: jest.fn(),
  })),
  AdminConfirmSignUpCommand: jest.fn(),
  AdminAddUserToGroupCommand: jest.fn(),
  AdminDeleteUserCommand: jest.fn(),
}));

// Mock JWT verifier
jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn().mockReturnValue({
      verify: jest.fn(),
    }),
  },
}));

describe('/api/admin/approve', () => {
  let mockVerify: jest.Mock;
  let mockSend: jest.Mock;

  beforeEach(async () => {
    jest.clearAllMocks();
    const { CognitoJwtVerifier } = await import('aws-jwt-verify');
    const { CognitoIdentityProviderClient } = await import('@aws-sdk/client-cognito-identity-provider');

    mockVerify = CognitoJwtVerifier.create().verify;

    // Create a fresh mock for send
    mockSend = jest.fn().mockResolvedValue({});
    CognitoIdentityProviderClient.mockImplementation(() => ({
      send: mockSend,
    }));
  });

  it('returns 401 when token is missing', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: {},
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Unauthorized' });
  });

  it('returns 401 when JWT verification fails with expired token', async () => {
    const expiredError = new Error('Token expired');
    expiredError.name = 'JwtExpiredError';
    mockVerify.mockRejectedValue(expiredError);

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=expired_token' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Invalid or expired token' });
  });

  it('returns 401 when JWT verification fails with invalid signature', async () => {
    const signatureError = new Error('Invalid signature');
    signatureError.name = 'JwtInvalidSignatureError';
    mockVerify.mockRejectedValue(signatureError);

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=invalid_token' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Invalid or expired token' });
  });

  it('returns 400 when JWT is malformed', async () => {
    const parseError = new Error('Malformed token');
    parseError.name = 'JwtParseError';
    mockVerify.mockRejectedValue(parseError);

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=malformed' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(400);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Malformed token' });
  });

  it('returns 403 when user is not admin', async () => {
    mockVerify.mockResolvedValue({ 'cognito:groups': ['users'] });

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=valid_token' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(403);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Admin access required' });
  });

  it('returns 503 when AWS Cognito service fails', async () => {
    mockVerify.mockResolvedValue({ 'cognito:groups': ['admin'] });

    const awsError = {
      name: 'ServiceUnavailableException',
      $metadata: { httpStatusCode: 503 },
      message: 'Service unavailable',
    };

    // Mock send to reject on first call
    mockSend.mockRejectedValueOnce(awsError);

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=valid_token' },
      body: { username: 'testuser', action: 'approve' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(503);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Service temporarily unavailable' });
  });

  it('returns 500 for unknown errors', async () => {
    mockVerify.mockRejectedValue(new Error('Unknown error'));

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=valid_token' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(500);
    expect(JSON.parse(res._getData())).toEqual({ error: 'Failed to process action' });
  });

  it('successfully approves user when admin', async () => {
    mockVerify.mockResolvedValue({ 'cognito:groups': ['admin'] });
    mockSend.mockResolvedValue({});

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=valid_token' },
      body: { username: 'testuser', action: 'approve' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    expect(JSON.parse(res._getData())).toEqual({ message: 'User approved' });
  });

  it('successfully denies user when admin', async () => {
    mockVerify.mockResolvedValue({ 'cognito:groups': ['admin'] });
    mockSend.mockResolvedValue({});

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=valid_token' },
      body: { username: 'testuser', action: 'deny' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    expect(JSON.parse(res._getData())).toEqual({ message: 'User denied and deleted' });
  });
});
