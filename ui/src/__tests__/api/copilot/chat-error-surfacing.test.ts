/**
 * Tests for chat.ts API endpoint — invocation errors must be VISIBLE to the user.
 *
 * Regression guard for issue #250: invocation-stage failures used to return a
 * bare 500 JSON body, which CopilotKit silently drops (no message renders, no
 * error shows — the user just gets nothing). Errors thrown while invoking the
 * agent must instead come back as a well-formed CopilotKit assistant message so
 * the user sees that something went wrong and can retry.
 */

import { createMocks } from 'node-mocks-http';
import { BedrockAgentCoreClient } from '@aws-sdk/client-bedrock-agentcore';
import { STSClient } from '@aws-sdk/client-sts';
import handler from '../../../pages/api/copilot/chat';

jest.mock('@/utils/logger', () => ({
  logInfo: jest.fn(),
  logError: jest.fn(),
  logDebug: jest.fn(),
  redact: jest.fn((v?: string | null) => (v ? `${v.slice(0, 2)}…(redacted)` : '<none>')),
}));

jest.mock('@aws-sdk/client-bedrock-agentcore');
jest.mock('@aws-sdk/client-sts');

const mockVerify = jest.fn();
jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn(() => ({ verify: mockVerify })),
  },
}));

// The dev-path invocation goes through fetchWithTimeout; rejecting it simulates
// a hung/failed backend (timeout abort, connection refused, 5xx...).
const mockFetchWithTimeout = jest.fn();
jest.mock('@/utils/fetchWithTimeout', () => ({
  fetchWithTimeout: (...args: unknown[]) => mockFetchWithTimeout(...args),
}));

const mockAgentCoreSend = jest.fn();
const mockStsSend = jest.fn();

function chatRequest(cookie = 'cognito_id_token=header.payload.signature') {
  return createMocks({
    method: 'POST',
    headers: { cookie },
    body: {
      operationName: 'generateCopilotResponse',
      variables: {
        data: {
          threadId: 'thread-errtest',
          messages: [{ textMessage: { role: 'user', content: 'analyze my whole infrastructure' } }],
        },
      },
    },
  });
}

describe('/api/copilot/chat - error surfacing (#250)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'development';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    (BedrockAgentCoreClient as jest.Mock).mockImplementation(() => ({ send: mockAgentCoreSend }));
    (STSClient as jest.Mock).mockImplementation(() => ({ send: mockStsSend }));
    mockStsSend.mockResolvedValue({ Account: '123456789012' });
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

  it('returns an invocation failure as a visible assistant message, not a bare 500', async () => {
    mockFetchWithTimeout.mockRejectedValue(new Error('The operation was aborted'));

    const { req, res } = chatRequest();
    await handler(req, res);

    // CopilotKit only renders 200 responses in the generateCopilotResponse
    // shape — anything else is silently dropped and the user sees nothing.
    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    const message = data.data.generateCopilotResponse.messages[0];
    expect(message.role).toBe('assistant');
    expect(message.__typename).toBe('TextMessageOutput');
    // The text must tell the user something went wrong and how to proceed.
    expect(message.content[0]).toMatch(/something went wrong|couldn.t complete/i);
    expect(message.content[0]).toMatch(/try again/i);
  });

  it('includes the request ID in the visible error so users can report it', async () => {
    mockFetchWithTimeout.mockRejectedValue(new Error('connect ECONNREFUSED'));

    const { req, res } = chatRequest();
    await handler(req, res);

    const data = JSON.parse(res._getData());
    const text = data.data.generateCopilotResponse.messages[0].content[0];
    expect(text).toMatch(/request id/i);
  });

  it('does not leak internal error details into the visible message', async () => {
    mockFetchWithTimeout.mockRejectedValue(
      new Error('connect ECONNREFUSED 10.0.2.17:8080 (task arn:aws:ecs:...)')
    );

    const { req, res } = chatRequest();
    await handler(req, res);

    const data = JSON.parse(res._getData());
    const text = data.data.generateCopilotResponse.messages[0].content[0];
    expect(text).not.toContain('ECONNREFUSED');
    expect(text).not.toContain('10.0.2.17');
  });

  it('surfaces production AccessDenied failures as a visible assistant message', async () => {
    process.env.NODE_ENV = 'production';
    process.env.AGENTCORE_RUNTIME_ID = 'runtime-errtest';
    process.env.COGNITO_CLIENT_ID = 'web-client';
    process.env.GBAW_TENANT_ID = 'tenant-a';
    process.env.GBAW_WORKSPACE_ID = 'workspace-a';
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    mockVerify.mockResolvedValue({
      token_use: 'access',
      sub: 'user123',
      client_id: 'web-client',
      email: 'test@example.com',
      'cognito:groups': ['users'],
    });
    const accessDenied = new Error('not authorized to invoke runtime');
    accessDenied.name = 'AccessDeniedException';
    mockAgentCoreSend.mockRejectedValue(accessDenied);

    const { req, res } = chatRequest(
      'cognito_access_token=access-token; cognito_id_token=id-token'
    );
    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    const message = data.data.generateCopilotResponse.messages[0];
    expect(message.role).toBe('assistant');
    expect(message.content[0]).toMatch(/something went wrong|couldn.t complete/i);
  });

  it('keeps plain HTTP errors for non-chat operations', async () => {
    // loadAgentState and friends are protocol plumbing — a fabricated chat
    // message would corrupt state; those keep JSON error responses.
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=header.payload.signature' },
      body: { operationName: 'bogusOperation', variables: { data: {} } },
    });

    await handler(req, res);
    expect(res._getStatusCode()).toBe(400);
  });
});
