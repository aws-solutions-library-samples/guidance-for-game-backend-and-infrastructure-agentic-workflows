/**
 * Chat proxy ↔ CopilotKit wire-protocol contract.
 *
 * WHY THIS EXISTS: chat.ts is a CUSTOM proxy that speaks CopilotKit's 1.10.x
 * GraphQL protocol (`operationName` + `variables.data.messages`). When the
 * client was silently bumped to 1.61 (AG-UI/SSE, `{method,params,body}`), the
 * proxy stopped recognizing requests and swallowed every message — with no test
 * catching it. These tests lock the contract from the proxy side:
 *   1. the real 1.10.x GraphQL shapes are handled (not 400 "Unknown operation");
 *   2. the 1.61 AG-UI shape is NOT silently accepted — it hits the explicit
 *      "Unknown operation" path, which is the signal to migrate the proxy.
 *
 * Pairs with copilotkit-version-pin.test.ts (which guards the installed version)
 * and the e2e chat smoke test (which exercises the real browser→proxy path).
 */
import { createMocks } from 'node-mocks-http';
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
  CognitoJwtVerifier: { create: jest.fn(() => ({ verify: mockVerify })) },
}));

describe('/api/copilot/chat - CopilotKit wire-protocol contract', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'development';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    mockVerify.mockResolvedValue({ sub: 'u1', email: 'user@example.com', 'cognito:groups': ['users'] });
  });
  afterEach(() => {
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
  });

  // ---- The protocol our proxy is built for: CopilotKit 1.10.x GraphQL ----

  it('handles availableAgents (1.10.x GraphQL) → 200 with the agent list', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=h.p.s' },
      body: { operationName: 'availableAgents', variables: {} },
    });
    await handler(req, res);
    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    expect(data.data.availableAgents).toBeDefined();
  });

  it('handles loadAgentState (1.10.x GraphQL) → 200 empty state (not 400)', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=h.p.s' },
      body: { operationName: 'loadAgentState', variables: { data: { threadId: 't1' } } },
    });
    await handler(req, res);
    expect(res._getStatusCode()).toBe(200);
    expect(JSON.parse(res._getData()).data.loadAgentState).toBeDefined();
  });

  it('recognizes generateCopilotResponse (1.10.x GraphQL) — not "Unknown operation"', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=h.p.s' },
      body: {
        operationName: 'generateCopilotResponse',
        variables: { data: { messages: [{ textMessage: { role: 'user', content: 'hi' } }] } },
      },
    });
    await handler(req, res);
    // The backend SDK is mocked so the call may 200 or 500, but it must NOT be
    // the 400 "Unknown operation" path — that would mean the proxy didn't even
    // recognize the operation (the exact symptom of the protocol mismatch).
    expect(res._getStatusCode()).not.toBe(400);
  });

  // ---- The protocol that BROKE us: CopilotKit 1.61 AG-UI single-endpoint ----

  it('does NOT silently accept the 1.61 AG-UI {method,params,body} envelope', async () => {
    // This is exactly what react-core 1.61 POSTs. Our 1.10.x proxy must treat it
    // as an unknown operation (400) — a loud signal — rather than appear to work
    // while dropping the message. If we ever migrate the proxy to AG-UI, THIS
    // test changes deliberately alongside that work.
    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_id_token=h.p.s' },
      body: { method: 'agent/run', params: { agentId: 'game-agent' }, body: { messages: [] } },
    });
    await handler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(JSON.parse(res._getData()).error).toBe('Unknown operation');
  });
});
