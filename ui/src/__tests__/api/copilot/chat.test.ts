/**
 * Tests for chat.ts API endpoint - JWT decoding error handling
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/copilot/chat';

// Mock dependencies
jest.mock('@/utils/logger', () => ({
  logInfo: jest.fn(),
  logError: jest.fn(),
}));

jest.mock('@aws-sdk/client-bedrock-agentcore');
jest.mock('@aws-sdk/client-sts');
jest.mock('aws-jwt-verify');

describe('/api/copilot/chat - JWT decoding', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'development';
  });

  it('handles malformed JWT with invalid format', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: {
        cookie: 'cognito_id_token=invalid.token'
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

    expect(res._getStatusCode()).toBe(400);
    const data = JSON.parse(res._getData());
    expect(data.error).toBe('Invalid token format');
  });

  it('handles JWT with invalid base64 encoding', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: {
        cookie: 'cognito_id_token=header.!!!invalid-base64!!!.signature'
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

    expect(res._getStatusCode()).toBe(400);
    const data = JSON.parse(res._getData());
    expect(data.error).toBe('Invalid token format');
  });

  it('handles JWT with invalid JSON', async () => {
    const invalidJson = Buffer.from('{invalid json}').toString('base64');

    const { req, res } = createMocks({
      method: 'POST',
      headers: {
        cookie: `cognito_id_token=header.${invalidJson}.signature`
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

    expect(res._getStatusCode()).toBe(400);
    const data = JSON.parse(res._getData());
    expect(data.error).toBe('Invalid token format');
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
    const payload = {
      sub: 'user123',
      email: 'test@example.com',
      'cognito:groups': []
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
});
