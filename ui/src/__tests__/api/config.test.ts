import { createMocks } from 'node-mocks-http';
import handler from '@/pages/api/config';

describe('/api/config session settings', () => {
  afterEach(() => {
    delete process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS;
    delete process.env.GBAW_SESSION_IDLE_REFRESH_SECONDS;
  });

  it('returns deployment-configured session lifetimes', () => {
    process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = '10';
    process.env.GBAW_SESSION_IDLE_REFRESH_SECONDS = '600';
    const { req, res } = createMocks({ method: 'GET' });

    handler(req, res);

    expect(JSON.parse(res._getData()).session).toEqual({
      absoluteLifetimeHours: 10,
      idleRefreshSeconds: 600,
    });
  });

  it('uses secure defaults for invalid values', () => {
    process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = 'invalid';
    process.env.GBAW_SESSION_IDLE_REFRESH_SECONDS = '0';
    const { req, res } = createMocks({ method: 'GET' });

    handler(req, res);

    expect(JSON.parse(res._getData()).session).toEqual({
      absoluteLifetimeHours: 8,
      idleRefreshSeconds: 900,
    });
  });
});
