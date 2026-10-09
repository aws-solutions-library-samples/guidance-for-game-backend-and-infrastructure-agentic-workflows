/**
 * chat.ts drives the real logger, which writes to console. This test spies on
 * the console sinks directly (it does NOT mock the logger) so a thread/identity
 * value interpolated into a log statement is caught at the real sink, including
 * when forwarded as an Error argument (#472, review item 7).
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/copilot/chat';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';

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

  it('never writes the raw threadId to any console sink', async () => {
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
  });
});
