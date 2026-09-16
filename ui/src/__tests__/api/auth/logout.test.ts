import { createMocks } from 'node-mocks-http';
import handler from '@/pages/api/auth/logout';

describe('/api/auth/logout', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'production';
  });

  it('clears tokens, refresh credential, and absolute deadline', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { host: 'app.example.com', origin: 'https://app.example.com' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const cookies = res.getHeader('Set-Cookie') as string[];
    expect(cookies).toHaveLength(4);
    expect(cookies.join(';')).toContain('cognito_access_token=');
    expect(cookies.join(';')).toContain('cognito_id_token=');
    expect(cookies.join(';')).toContain('cognito_refresh_token=');
    expect(cookies.join(';')).toContain('cognito_session_deadline=');
    expect(cookies.every((cookie) => cookie.includes('Max-Age=0'))).toBe(true);
  });

  it('blocks cross-origin logout requests', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      headers: { host: 'app.example.com', origin: 'https://evil.example.com' },
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(403);
    expect(res.getHeader('Set-Cookie')).toBeUndefined();
  });
});
