/**
 * Chart fence survives the real proxy path and is accepted by the contract (#255).
 *
 * This exercises the ACTUAL chat.ts proxy (production AgentCore SDK path): the
 * backend returns an assistant message that contains a ```chart fence, the
 * proxy unwraps the AgentCore JSON envelope + unescapes it, and formats it into
 * the CopilotKit message the browser renders. We assert the fence arrives byte-
 * intact and that the frontend contract (parseChartSpec) accepts it — proving
 * the producer (backend/deterministic cost report) and consumer (renderer)
 * agree across the proxy.
 */
import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/copilot/chat';
import { parseChartSpec } from '../../../components/charts/chartContract';

jest.mock('@/utils/logger', () => ({
  logInfo: jest.fn(),
  logError: jest.fn(),
  logDebug: jest.fn(),
  redact: jest.fn((v?: string | null) => (v ? '<redacted>' : '<none>')),
}));

const mockAgentCoreSend = jest.fn();
jest.mock('@aws-sdk/client-bedrock-agentcore', () => ({
  BedrockAgentCoreClient: jest.fn(() => ({ send: mockAgentCoreSend })),
  InvokeAgentRuntimeCommand: jest.fn((input: unknown) => ({ input })),
}));

jest.mock('@aws-sdk/client-sts', () => ({
  STSClient: jest.fn(() => ({ send: jest.fn().mockResolvedValue({ Account: '123456789012' }) })),
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

// A representative deterministic cost-report body, shaped exactly like what
// backend render_cost_report now emits: prose + a validated ```chart fence.
const chartJson = JSON.stringify({
  type: 'bar',
  version: '1.0',
  title: 'Top services by UnblendedCost (USD)',
  summary: 'Amazon EKS leads at USD 80.00 (32.0% of total).',
  unit: 'USD',
  x: { label: 'Service', values: ['Amazon EKS', 'Amazon GameLift', 'Other services'] },
  y: { label: 'USD' },
  series: [{ name: 'UnblendedCost', values: [80, 65, 105] }],
});
const backendMessage =
  '## Validated AWS Cost Explorer Report\n\n' +
  '**Total:** USD 250.00\n\n' +
  '```chart\n' +
  chartJson +
  '\n```';

function extractFence(text: string): string {
  const open = text.indexOf('```chart\n') + '```chart\n'.length;
  const close = text.indexOf('\n```', open);
  return text.slice(open, close);
}

describe('/api/copilot/chat - chart fence proxy round-trip', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'production';
    process.env.AGENTCORE_RUNTIME_ID = 'runtime-charts';
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
      'cognito:groups': ['users'],
      scope: 'openid profile',
    });
    mockIdVerify.mockResolvedValue({
      token_use: 'id',
      sub: 'subject-123',
      email: 'person@example.com',
      'cognito:groups': ['users'],
    });
    // AgentCore JSON-serializes string returns; the proxy JSON.parses that wrapper.
    mockAgentCoreSend.mockResolvedValue({
      response: {
        async *[Symbol.asyncIterator]() {
          yield Buffer.from(JSON.stringify(backendMessage));
        },
      },
    });
  });

  afterEach(() => {
    delete process.env.AGENTCORE_RUNTIME_ID;
    delete process.env.NODE_ENV;
    delete process.env.COGNITO_CLIENT_ID;
    delete process.env.GBAW_COGNITO_AUDIENCE;
    delete process.env.GBAW_TENANT_ID;
    delete process.env.GBAW_WORKSPACE_ID;
  });

  it('passes the chart fence through intact and the contract accepts it', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_access_token=access-token; cognito_id_token=id-token' },
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            threadId: 'thread-charts',
            messages: [{ textMessage: { role: 'user', content: 'show my costs as a chart' } }],
          },
        },
      },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const body = JSON.parse(res._getData());
    const content = body.data.generateCopilotResponse.messages[0].content[0] as string;

    // Fence survived the envelope unwrap + unescape.
    expect(content).toContain('```chart');
    // And the frontend contract accepts the exact payload that arrived.
    const spec = parseChartSpec(extractFence(content));
    expect(spec).not.toBeNull();
    expect(spec!.type).toBe('bar');
    expect(spec!.series[0].values).toEqual([80, 65, 105]);
  });
});
