/**
 * Tests for /api/auth/user endpoint
 */

import { createMocks } from 'node-mocks-http';
import handler from '../../../pages/api/auth/user';

describe('/api/auth/user', () => {
  it('should return 405 for non-GET requests', async () => {
    const { req, res } = createMocks({
      method: 'POST',
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(405);
    expect(JSON.parse(res._getData())).toEqual({
      error: 'Method not allowed'
    });
  });

  it('should return 401 when no token is present', async () => {
    const { req, res } = createMocks({
      method: 'GET',
      cookies: {}
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(JSON.parse(res._getData())).toEqual({
      error: 'No authentication token found'
    });
  });

  it('should parse user info from valid JWT token', async () => {
    // Create a mock JWT token (header.payload.signature)
    const mockPayload = {
      email: 'test@example.com',
      sub: 'user-123',
      'cognito:groups': ['admin']
    };

    const encodedPayload = Buffer.from(JSON.stringify(mockPayload)).toString('base64');
    const mockToken = `header.${encodedPayload}.signature`;

    const { req, res } = createMocks({
      method: 'GET',
      cookies: {
        cognito_id_token: mockToken
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const responseData = JSON.parse(res._getData());
    expect(responseData).toEqual({
      username: 'test',
      email: 'test@example.com',
      isAdmin: true,
      sub: 'user-123'
    });
  });

  it('should extract username from email correctly', async () => {
    const mockPayload = {
      email: 'john.doe@company.com',
      sub: 'user-456'
    };

    const encodedPayload = Buffer.from(JSON.stringify(mockPayload)).toString('base64');
    const mockToken = `header.${encodedPayload}.signature`;

    const { req, res } = createMocks({
      method: 'GET',
      cookies: {
        cognito_id_token: mockToken
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const responseData = JSON.parse(res._getData());
    expect(responseData.username).toBe('john.doe');
    expect(responseData.email).toBe('john.doe@company.com');
    expect(responseData.isAdmin).toBe(false);
  });

  it('should handle malformed JWT token gracefully', async () => {
    const { req, res } = createMocks({
      method: 'GET',
      cookies: {
        cognito_id_token: 'invalid.token'
      }
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(500);
    expect(JSON.parse(res._getData())).toEqual({
      error: 'Failed to parse user information'
    });
  });
});
