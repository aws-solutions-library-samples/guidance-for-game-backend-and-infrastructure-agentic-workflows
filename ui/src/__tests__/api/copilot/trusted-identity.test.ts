import { createMocks } from 'node-mocks-http';
import { InvokeAgentRuntimeCommand } from '@aws-sdk/client-bedrock-agentcore';
import { logDebug, logError, logInfo } from '@/utils/logger';
import handler from '../../../pages/api/copilot/chat';

jest.mock('@/utils/logger', () => ({
  logInfo: jest.fn(),
  logError: jest.fn(),
  logDebug: jest.fn(),
  redact: jest.fn((value?: string | null) => (value ? '<redacted>' : '<none>')),
}));

const mockAgentCoreSend = jest.fn();
jest.mock('@aws-sdk/client-bedrock-agentcore', () => ({
  BedrockAgentCoreClient: jest.fn(() => ({ send: mockAgentCoreSend })),
  InvokeAgentRuntimeCommand: jest.fn((input: unknown) => ({ input })),
}));

jest.mock('@aws-sdk/client-sts', () => ({
  STSClient: jest.fn(() => ({
    send: jest.fn().mockResolvedValue({ Account: '123456789012' }),
  })),
  GetCallerIdentityCommand: jest.fn((input: unknown) => ({ input })),
}));

const mockAccessVerify = jest.fn();
const mockIdVerify = jest.fn();
jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn((config: { tokenUse: string }) => ({
      verify: config.tokenUse === 'access' ? mockAccessVerify : mockIdVerify,
    })),
  },
}));

function request(dataOverrides: Record<string, unknown> = {}) {
  return createMocks({
    method: 'POST',
    headers: {
      cookie: 'cognito_access_token=access-token; cognito_id_token=id-token',
    },
    body: {
      operationName: 'generateCopilotResponse',
      variables: {
        data: {
          threadId: 'thread-identity',
          messages: [
            {
              textMessage: {
                role: 'user',
                content: 'show approved IaC for person@example.com',
              },
            },
          ],
          ...dataOverrides,
        },
      },
    },
  });
}

describe('/api/copilot/chat - trusted identity propagation', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'production';
    process.env.AGENTCORE_RUNTIME_ID = 'runtime-identity';
    process.env.AWS_REGION = 'us-west-2';
    process.env.COGNITO_CLIENT_ID = 'web-client';
    process.env.GBAW_COGNITO_AUDIENCE = 'web-client';
    process.env.GBAW_TENANT_ID = 'tenant-a';
    process.env.GBAW_WORKSPACE_ID = 'workspace-a';
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;

    mockAccessVerify.mockResolvedValue({
      token_use: 'access',
      sub: 'subject-123',
      client_id: 'web-client',
      'cognito:groups': ['users', 'source-readers'],
      scope: 'openid profile',
    });
    mockIdVerify.mockResolvedValue({
      token_use: 'id',
      sub: 'subject-123',
      email: 'person@example.com',
      'cognito:groups': ['admin'],
    });
    mockAgentCoreSend.mockResolvedValue({
      response: {
        async *[Symbol.asyncIterator]() {
          yield Buffer.from(JSON.stringify('ok'));
        },
      },
    });
  });

  afterEach(() => {
    delete process.env.AGENTCORE_RUNTIME_ID;
    delete process.env.AWS_REGION;
    delete process.env.COGNITO_CLIENT_ID;
    delete process.env.GBAW_COGNITO_AUDIENCE;
    delete process.env.GBAW_TENANT_ID;
    delete process.env.GBAW_WORKSPACE_ID;
  });

  it('propagates access claims and deployment bindings while ignoring body identity', async () => {
    const { req, res } = request({
      user_context: {
        user_id: 'attacker',
        groups: ['admin'],
        tenant: 'other-tenant',
        workspace: 'other-workspace',
      },
      tenant: 'other-tenant',
      workspace: 'other-workspace',
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const commandInput = (InvokeAgentRuntimeCommand as jest.Mock).mock.calls[0][0];
    const payload = JSON.parse(Buffer.from(commandInput.payload).toString('utf-8'));

    expect(payload.user_context).toMatchObject({
      user_id: 'subject-123',
      client_id: 'web-client',
      audience: 'web-client',
      groups: ['users', 'source-readers'],
      scopes: ['openid', 'profile'],
      tenant: 'tenant-a',
      workspace: 'workspace-a',
      is_admin: false,
    });
    expect(payload.user_context).not.toHaveProperty('email');
    expect(payload.user_context).not.toHaveProperty('display_name');

    const logs = JSON.stringify([
      ...(logInfo as jest.Mock).mock.calls,
      ...(logDebug as jest.Mock).mock.calls,
      ...(logError as jest.Mock).mock.calls,
    ]);
    expect(logs).not.toContain('person@example.com');
    expect(logs).not.toContain('access-token');
    expect(logs).not.toContain('id-token');
  });

  it('uses access-token groups rather than ID-token groups for authorization', async () => {
    mockAccessVerify.mockResolvedValue({
      token_use: 'access',
      sub: 'subject-123',
      client_id: 'web-client',
      'cognito:groups': [],
    });

    const { req, res } = request();
    await handler(req, res);

    expect(res._getStatusCode()).toBe(403);
    expect(mockIdVerify).not.toHaveBeenCalled();
    expect(mockAgentCoreSend).not.toHaveBeenCalled();
  });

  it('rejects an expired access token before reading identity claims', async () => {
    mockAccessVerify.mockRejectedValue(new Error('Token expired'));

    const { req, res } = request();
    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData()).error).toBe('Unauthorized');
  });

  it('fails closed when tenant/workspace configuration is missing', async () => {
    delete process.env.GBAW_WORKSPACE_ID;

    const { req, res } = request();
    await handler(req, res);

    expect(res._getStatusCode()).toBe(503);
    expect(JSON.parse(res._getData()).error).toBe('Identity configuration unavailable');
    expect(mockAgentCoreSend).not.toHaveBeenCalled();
  });
});
