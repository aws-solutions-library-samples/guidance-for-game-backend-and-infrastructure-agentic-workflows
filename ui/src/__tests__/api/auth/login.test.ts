import { createMocks } from 'node-mocks-http';

const mockAccessVerify = jest.fn();
const mockIdVerify = jest.fn();

jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn((options: { tokenUse: string }) => ({
      verify: options.tokenUse === 'access' ? mockAccessVerify : mockIdVerify,
    })),
  },
}));

jest.mock('@/utils/logger', () => ({ logError: jest.fn() }));

import handler from '@/pages/api/auth/login';

describe('/api/auth/login', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'production';
    process.env.COGNITO_USER_POOL_ID = 'pool-1';
    process.env.COGNITO_CLIENT_ID = 'client-1';
    process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = '8';
    const now = Math.floor(Date.now() / 1000);
    mockAccessVerify.mockResolvedValue({ sub: 'user-1', exp: now + 3600 });
    mockIdVerify.mockResolvedValue({ sub: 'user-1', exp: now + 3600 });
  });

  afterEach(() => {
    delete process.env.COGNITO_USER_POOL_ID;
    delete process.env.COGNITO_CLIENT_ID;
    delete process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS;
  });

  function request(body: Record<string, unknown>, origin = 'https://app.example.com') {
    return createMocks({
      method: 'POST',
      headers: { host: 'app.example.com', origin },
      body,
    });
  }

  it('verifies tokens and stores short-lived HttpOnly cookies', async () => {
    const { req, res } = request({ accessToken: 'access', idToken: 'id', refreshToken: 'refresh' });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    expect(mockAccessVerify).toHaveBeenCalledWith('access');
    expect(mockIdVerify).toHaveBeenCalledWith('id');
    const cookies = res.getHeader('Set-Cookie') as string[];
    expect(cookies).toHaveLength(4);
    expect(cookies.join(';')).toContain('cognito_refresh_token=refresh');
    expect(cookies.join(';')).toContain('cognito_session_deadline=');
    expect(cookies.every((cookie) => cookie.includes('HttpOnly'))).toBe(true);
    expect(res.getHeader('Cache-Control')).toBe('no-store');
  });

  it('rejects incomplete sessions', async () => {
    const { req, res } = request({ accessToken: 'access' });
    await handler(req, res);
    expect(res._getStatusCode()).toBe(400);
    expect(mockAccessVerify).not.toHaveBeenCalled();
  });

  it('rejects forged or expired tokens without setting cookies', async () => {
    mockAccessVerify.mockRejectedValue(new Error('expired'));
    const { req, res } = request({ accessToken: 'bad', idToken: 'id', refreshToken: 'refresh' });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(res.getHeader('Set-Cookie')).toBeUndefined();
  });

  it('rejects access and ID tokens for different subjects', async () => {
    mockIdVerify.mockResolvedValue({ sub: 'user-2', exp: Math.floor(Date.now() / 1000) + 3600 });
    const { req, res } = request({ accessToken: 'access', idToken: 'id', refreshToken: 'refresh' });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
  });

  it('blocks cross-origin login requests', async () => {
    const { req, res } = request(
      { accessToken: 'access', idToken: 'id', refreshToken: 'refresh' },
      'https://evil.example.com',
    );
    await handler(req, res);
    expect(res._getStatusCode()).toBe(403);
  });
});
