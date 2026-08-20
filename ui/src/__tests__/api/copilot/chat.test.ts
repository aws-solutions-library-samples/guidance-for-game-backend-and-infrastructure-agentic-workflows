/**
 * Tests for chat.ts API endpoint - JWT decoding error handling
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/copilot/chat';

// Mock dependencies
jest.mock('@/utils/logger', () => ({
  logInfo: jest.fn(),
  logError: jest.fn(),
  logDebug: jest.fn(),
  // redact must return a string (it's interpolated into log messages); the real
  // impl keeps a short prefix, the mock just needs to not be undefined.
  redact: jest.fn((v?: string | null) => (v ? `${v.slice(0, 2)}…(redacted)` : '<none>')),
}));

jest.mock('@aws-sdk/client-bedrock-agentcore');
jest.mock('@aws-sdk/client-sts');

// Shared verifier mock: chat.ts builds verifiers via CognitoJwtVerifier.create();
// each created verifier exposes this `mockVerify` so tests can control whether the
// ID token verifies (returns claims) or is rejected (throws).
const mockVerify = jest.fn();
jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn(() => ({ verify: mockVerify })),
  },
}));

describe('/api/copilot/chat - JWT decoding', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'development';
    // Local dev: the access-token check is skipped only via the explicit bypass
    // (NEXT_PUBLIC_SKIP_AUTH=true), which is what .env.local ships. NODE_ENV alone
    // no longer skips. ID-token verification still runs regardless of this flag.
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    // Default: a valid, approved ID token (tests override as needed).
    mockVerify.mockResolvedValue({
      sub: 'user123',
      email: 'test@example.com',
      'cognito:groups': ['users'],
    });
  });

  afterEach(() => {
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    delete process.env.AGENTCORE_RUNTIME_ID;
    delete process.env.COGNITO_CLIENT_ID;
    delete process.env.GBAW_TENANT_ID;
    delete process.env.GBAW_WORKSPACE_ID;
  });

  // An ID token that fails cryptographic verification (bad signature, wrong
  // token_use, malformed, expired) must be REJECTED with 401 — not raw-decoded.
  it.each([
    ['malformed format', 'cognito_id_token=invalid.token'],
    ['invalid signature/base64', 'cognito_id_token=header.payload.bad-signature'],
    ['expired/invalid token', 'cognito_id_token=header.payload.signature'],
  ])('rejects an ID token that fails verification (%s)', async (_label, cookie) => {
    mockVerify.mockRejectedValue(new Error('JwtInvalidSignatureError'));

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie },
      body: {
        operationName: 'generateCopilotResponse',
        variables: { data: { messages: [{ textMessage: { role: 'user', content: 'test' } }] } }
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    const data = JSON.parse(res._getData());
    expect(data.error).toBe('Invalid token');
  });

  it('successfully decodes valid JWT', async () => {
    const payload = {
      sub: 'user123',
      email: 'test@example.com',
      'cognito:groups': ['users']
    };
    const validPayload = Buffer.from(JSON.stringify(payload)).toString('base64');

    const { req, res } = createMocks({
      method: 'POST',
      headers: {
        cookie: `cognito_id_token=header.${validPayload}.signature`
      },
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [{
              textMessage: { role: 'user', content: 'test message' }
            }]
          }
        }
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).not.toBe(400);
  });

  it('returns 403 when user not approved', async () => {
    process.env.NODE_ENV = 'production';
    process.env.AGENTCORE_RUNTIME_ID = 'runtime-test';
    process.env.COGNITO_CLIENT_ID = 'web-client';
    process.env.GBAW_TENANT_ID = 'tenant-a';
    process.env.GBAW_WORKSPACE_ID = 'workspace-a';
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    // Verified access token, but no approved group.
    mockVerify.mockResolvedValue({
      token_use: 'access',
      sub: 'user123',
      client_id: 'web-client',
      'cognito:groups': [],
    });

    const { req, res } = createMocks({
      method: 'POST',
      headers: {
        cookie: 'cognito_access_token=access-token; cognito_id_token=id-token'
      },
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [{
              textMessage: { role: 'user', content: 'test' }
            }]
          }
        }
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(403);
    const data = JSON.parse(res._getData());
    expect(data.error).toBe('Account pending approval');

  });

  it('fails closed with 401 when auth is real but no trusted principal is built', async () => {
    // Regression guard for the #320 approval-gate finding: the admin/users gate must not be
    // silently skipped when the access token verifies "ok" but yields no payload/principal
    // outside the local-dev bypass. Simulate a non-prod HOSTED env (no SKIP_AUTH) where the
    // verifier resolves to a null-ish payload — the request must be rejected, not proceed.
    process.env.NODE_ENV = 'production';
    process.env.AGENTCORE_RUNTIME_ID = 'runtime-test';
    process.env.COGNITO_CLIENT_ID = 'web-client';
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    // Access token verifies but carries no usable identity claims → buildTrustedPrincipal is
    // never reached because accessPayload is null; the fail-closed guard must catch it.
    mockVerify.mockResolvedValue(null);

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_access_token=access-token' },
      body: {
        operationName: 'generateCopilotResponse',
        variables: { data: { messages: [{ textMessage: { role: 'user', content: 'test' } }] } }
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData()).error).toBe('Unauthorized');
  });

  it('handles loadAgentState with 200 + empty state (not a 400)', async () => {
    // CopilotKit issues loadAgentState on page load; it must not 400 (#60).
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=header.payload.signature' },
      body: {
        operationName: 'loadAgentState',
        variables: { data: { threadId: 'thread-abc', agentName: 'game-agent' } }
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    expect(data.data.loadAgentState.threadExists).toBe(false);
    expect(data.data.loadAgentState.threadId).toBe('thread-abc');
    expect(data.data.loadAgentState.messages).toBe('[]');
  });

  it('still rejects a genuinely unknown operation with 400', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=header.payload.signature' },
      body: { operationName: 'bogusOperation', variables: { data: {} } }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(400);
    expect(JSON.parse(res._getData()).error).toBe('Unknown operation');
  });

  // A non-production HOSTED env (staging/preview) is also NODE_ENV !== 'production'.
  // Without the explicit NEXT_PUBLIC_SKIP_AUTH bypass it must NOT skip auth — a
  // request with no verified access token is rejected, not silently let through.
  it('enforces the access-token check in non-prod when SKIP_AUTH is not set', async () => {
    delete process.env.NEXT_PUBLIC_SKIP_AUTH; // hosted staging/preview, no local bypass

    const { req, res } = createMocks({
      method: 'POST',
      headers: {}, // no cognito_access_token cookie
      body: {
        operationName: 'generateCopilotResponse',
        variables: { data: { messages: [{ textMessage: { role: 'user', content: 'test' } }] } }
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData()).error).toBe('Unauthorized');
  });
});
