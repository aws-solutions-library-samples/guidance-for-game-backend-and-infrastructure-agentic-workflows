/**
 * Tests for chat.ts - Log injection vulnerability (CWE-117)
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/copilot/chat';
import * as logger from '../../../utils/logger';

// Mock logger
jest.mock('../../../utils/logger', () => ({
  logInfo: jest.fn(),
  logError: jest.fn(),
}));

// Mock AWS SDK
jest.mock('@aws-sdk/client-bedrock-agentcore');
jest.mock('@aws-sdk/client-sts');
jest.mock('aws-jwt-verify');

describe('chat.ts - Log injection prevention', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // Model local dev so the handler proceeds past the access-token check into the
    // method-handling logs these tests assert on (NODE_ENV alone no longer skips).
    process.env.NODE_ENV = 'development';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
  });

  afterEach(() => {
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
  });

  it('sanitizes newline characters in req.method to prevent log injection', async () => {
    const { req, res } = createMocks({
      method: 'GET\n[INJECTED] Fake admin access granted',
      headers: {},
    });

    await handler(req, res);

    // Check that logInfo was called
    expect(logger.logInfo).toHaveBeenCalled();

    // Get all logInfo calls
    const logCalls = (logger.logInfo as jest.Mock).mock.calls;

    // Find the call that logs the method
    const methodLogCall = logCalls.find(call =>
      call[0].includes('Received') && call[0].includes('request to /api/copilot/chat')
    );

    expect(methodLogCall).toBeDefined();

    // Verify newlines are removed (text after newline will be concatenated)
    const logMessage = methodLogCall[0];
    expect(logMessage).not.toContain('\n');
    expect(logMessage).not.toContain('\r');
    // The injected text will still be there, but on same line (no log injection)
    expect(logMessage).toContain('GET[INJECTED]');
  });

  it('sanitizes carriage return characters in req.method', async () => {
    const { req, res } = createMocks({
      method: 'POST\r\n[MALICIOUS] Security breach detected',
      headers: {},
    });

    await handler(req, res);

    const logCalls = (logger.logInfo as jest.Mock).mock.calls;
    const methodLogCall = logCalls.find(call =>
      call[0].includes('Received') && call[0].includes('request to /api/copilot/chat')
    );

    expect(methodLogCall).toBeDefined();
    const logMessage = methodLogCall[0];
    expect(logMessage).not.toContain('\r');
    expect(logMessage).not.toContain('\n');
    // The text will be concatenated but no new log line created
    expect(logMessage).toContain('POST[MALICIOUS]');
  });

  it('handles undefined req.method gracefully', async () => {
    const { req, res } = createMocks({
      headers: {},
    });

    // Manually set method to undefined
    req.method = undefined;

    await handler(req, res);

    const logCalls = (logger.logInfo as jest.Mock).mock.calls;
    const methodLogCall = logCalls.find(call =>
      call[0].includes('Received') && call[0].includes('request to /api/copilot/chat')
    );

    expect(methodLogCall).toBeDefined();
    const logMessage = methodLogCall[0];
    expect(logMessage).toContain('UNKNOWN');
  });

  it('allows normal HTTP methods through without modification', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: {},
    });

    await handler(req, res);

    const logCalls = (logger.logInfo as jest.Mock).mock.calls;
    const methodLogCall = logCalls.find(call =>
      call[0].includes('Received') && call[0].includes('request to /api/copilot/chat')
    );

    expect(methodLogCall).toBeDefined();
    const logMessage = methodLogCall[0];
    expect(logMessage).toContain('POST');
  });

  it('sanitizes method in unsupported method error log', async () => {
    const { req, res } = createMocks({
      method: 'DELETE\n[FAKE] Admin logged in',
      headers: {},
      body: {},
    });

    await handler(req, res);

    const errorCalls = (logger.logError as jest.Mock).mock.calls;
    const methodErrorCall = errorCalls.find(call =>
      call[0].includes('Unsupported method')
    );

    expect(methodErrorCall).toBeDefined();
    const logMessage = methodErrorCall[0];
    expect(logMessage).not.toContain('\n');
    // Text concatenated but no log injection
    expect(logMessage).toContain('DELETE[FAKE]');
  });

  it('sanitizes method in GET/HEAD request log', async () => {
    const { req, res } = createMocks({
      method: 'GET\n[INJECTED] Password reset',
      headers: {},
    });

    await handler(req, res);

    const logCalls = (logger.logInfo as jest.Mock).mock.calls;
    // Check the initial request log instead
    const methodLogCall = logCalls.find(call =>
      call[0].includes('Received') && call[0].includes('request to /api/copilot/chat')
    );

    expect(methodLogCall).toBeDefined();
    const logMessage = methodLogCall[0];
    expect(logMessage).not.toContain('\n');
    // Text concatenated but no log injection
    expect(logMessage).toContain('GET[INJECTED]');
  });
});
