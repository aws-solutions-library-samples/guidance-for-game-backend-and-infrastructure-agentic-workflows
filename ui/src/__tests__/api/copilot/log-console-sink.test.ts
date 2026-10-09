/**
 * chat.ts drives the real logger, which writes to console. This test spies on
 * the console sinks directly (it does NOT mock the logger) so a thread/identity
 * value interpolated into a log statement is caught at the real sink, including
 * when forwarded as an Error argument (#472, review item 7).
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/copilot/chat';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { STSClient } from '@aws-sdk/client-sts';

jest.mock('@aws-sdk/client-sts');
jest.mock('aws-jwt-verify');
jest.mock('@/utils/fetchWithTimeout', () => ({
  fetchWithTimeout: jest.fn(),
}));

const THREAD_MARKER = 'thread-ZZMARKER-00000000-0000-0000-0000-000000000000';

const originalConsole = { ...console };

describe('chat.ts - real console sink carries no raw identity', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'development';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    process.env.BACKEND_URL = 'http://localhost:8080';

    console.log = jest.fn();
    console.error = jest.fn();
    console.warn = jest.fn();

    // Keep the handler off the network: a successful local backend response so
    // the request reaches and passes the thread-logging statements.
    (fetchWithTimeout as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify('a safe assistant reply'),
      json: async () => 'a safe assistant reply',
      headers: new Map(),
    });
  });

  afterEach(() => {
    Object.assign(console, originalConsole);
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    delete process.env.BACKEND_URL;
  });

  function allConsoleText(): string {
    const sinks = [console.log, console.error, console.warn] as jest.Mock[];
    return sinks
      .flatMap((sink) => sink.mock.calls)
      .flat()
      // Include non-string arguments (e.g. an Error forwarded to logError) as
      // their string form so a leak through them is still visible.
      .map((arg) => (typeof arg === 'string' ? arg : String((arg as { stack?: string })?.stack ?? arg)))
      .join(' ');
  }

  it('never writes the raw threadId to any console sink (local path)', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: {},
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            threadId: THREAD_MARKER,
            messages: [
              { textMessage: { role: 'user', content: 'How are my fleets doing?' } },
            ],
          },
        },
      },
    });

    await handler(req, res);

    const output = allConsoleText();
    expect(output).not.toContain(THREAD_MARKER);
    expect(output).not.toContain('ZZMARKER');
    // Positive assertions so an early return cannot pass silently: the request
    // ran to a 200 and emitted the redacted thread line.
    expect(res._getStatusCode()).toBe(200);
    expect(output).toContain('Thread: th…(redacted)');
  });

  it('never writes the raw threadId on the JWT runtime path', async () => {
    // Drive the production JWT path: a verified access/id token, an approved
    // group, and a configured runtime, then invoke the AgentCore runtime.
    process.env.NODE_ENV = 'production';
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    process.env.AGENTCORE_RUNTIME_ID = 'rt-test';
    process.env.COGNITO_CLIENT_ID = 'client-test';
    process.env.GBAW_COGNITO_AUDIENCE = 'client-test';
    process.env.GBAW_TENANT_ID = 'tenant-test';
    process.env.GBAW_WORKSPACE_ID = 'workspace-test';
    process.env.AWS_REGION = 'us-west-2';

    // The access and id verifiers both resolve to an approved subject.
    const verify = jest.fn().mockResolvedValue({
      sub: 'sub-approved',
      'cognito:groups': ['users'],
      email: 'player@example.com',
      client_id: 'client-test',
      token_use: 'access',
    });
    (CognitoJwtVerifier.create as jest.Mock).mockReturnValue({ verify });

    // getAccountId() calls STS to build the runtime ARN; return a synthetic
    // account so the invocation path proceeds to the trace-id log line.
    (STSClient as unknown as jest.Mock).mockImplementation(() => ({
      send: jest.fn().mockResolvedValue({ Account: '123456789012' }),
    }));

    // The runtime responds 200 with a trace-id header carrying a CR/LF marker
    // so the test also proves the trace id is normalized onto a single line.
    const runtimeHeaders = new Map<string, string>([
      ['x-amzn-trace-id', 'Root=1-trace\r\nINJECTED-TRACE-LINE'],
    ]);
    (fetchWithTimeout as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify('a safe assistant reply'),
      json: async () => 'a safe assistant reply',
      headers: { get: (k: string) => runtimeHeaders.get(k.toLowerCase()) ?? null },
    });

    const { req, res } = createMocks({
      method: 'POST',
      headers: { cookie: 'cognito_access_token=atoken; cognito_id_token=itoken' },
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            threadId: THREAD_MARKER,
            messages: [{ textMessage: { role: 'user', content: 'How are my fleets doing?' } }],
          },
        },
      },
    });

    await handler(req, res);

    const output = allConsoleText();
    expect(output).not.toContain(THREAD_MARKER);
    expect(output).not.toContain('ZZMARKER');
    // The runtime trace id is logged normalized (single line), not redacted,
    // and its CR/LF cannot forge a second log line (each control char collapses
    // to one space, so the CRLF becomes two spaces).
    expect(output).toContain('runtime trace Root=1-trace  INJECTED-TRACE-LINE');
    expect(output).not.toContain('\n');
    expect(output).not.toContain('\r');

    delete process.env.AGENTCORE_RUNTIME_ID;
    delete process.env.COGNITO_CLIENT_ID;
    delete process.env.GBAW_COGNITO_AUDIENCE;
    delete process.env.GBAW_TENANT_ID;
    delete process.env.GBAW_WORKSPACE_ID;
  });
});
