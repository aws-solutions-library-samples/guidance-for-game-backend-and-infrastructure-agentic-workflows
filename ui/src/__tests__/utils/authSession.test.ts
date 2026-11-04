import {
  absoluteRemainingSeconds,
  absoluteSessionLifetimeSeconds,
  clearAuthCookies,
  createInitialSessionCookies,
  createRefreshedTokenCookies,
} from '@/utils/authSession';

describe('authSession cookie policy', () => {
  const originalLifetime = process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS;

  afterEach(() => {
    if (originalLifetime === undefined) delete process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS;
    else process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = originalLifetime;
  });

  it('derives token cookie lifetimes and enforces the configured absolute boundary', () => {
    process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = '8';
    const cookies = createInitialSessionCookies({
      accessToken: 'access',
      idToken: 'id',
      refreshToken: 'refresh',
      accessPayload: { sub: 'user-1', exp: 4_600 },
      idPayload: { sub: 'user-1', exp: 4_000 },
      nowSeconds: 1_000,
    });

    expect(cookies).toHaveLength(4);
    expect(cookies[0]).toContain('cognito_access_token=access');
    expect(cookies[0]).toContain('Max-Age=3600');
    expect(cookies[1]).toContain('Max-Age=3000');
    expect(cookies[2]).toContain('cognito_refresh_token=refresh');
    expect(cookies[2]).toContain('Max-Age=28800');
    expect(cookies[3]).toContain('cognito_session_deadline=29800');
    for (const cookie of cookies) {
      expect(cookie).toContain('HttpOnly');
      expect(cookie).toContain('SameSite=Lax');
    }
  });

  it('rejects token pairs for different subjects', () => {
    expect(() => createInitialSessionCookies({
      accessToken: 'access',
      idToken: 'id',
      refreshToken: 'refresh',
      accessPayload: { sub: 'user-1', exp: 2_000 },
      idPayload: { sub: 'user-2', exp: 2_000 },
      nowSeconds: 1_000,
    })).toThrow('same subject');
  });

  it('caps refreshed token cookies at the original absolute deadline', () => {
    const cookies = createRefreshedTokenCookies({
      accessToken: 'new-access',
      idToken: 'new-id',
      accessPayload: { sub: 'user-1', exp: 5_000 },
      idPayload: { sub: 'user-1', exp: 5_000 },
      absoluteRemainingSeconds: 300,
      nowSeconds: 1_000,
    });

    expect(cookies).toHaveLength(2);
    expect(cookies.every((cookie) => cookie.includes('Max-Age=300'))).toBe(true);
    expect(cookies.join(';')).not.toContain('cognito_refresh_token');
  });

  it('parses and clamps deadline lifetime', () => {
    process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = '8';
    expect(absoluteRemainingSeconds('1400', 1000)).toBe(400);
    expect(absoluteRemainingSeconds('999999', 1000)).toBe(absoluteSessionLifetimeSeconds());
    expect(absoluteRemainingSeconds('bad', 1000)).toBe(0);
    expect(absoluteRemainingSeconds('999', 1000)).toBe(0);
  });

  it('clears all authentication and deadline cookies', () => {
    const cookies = clearAuthCookies();
    expect(cookies).toHaveLength(4);
    expect(cookies.every((cookie) => cookie.includes('Max-Age=0'))).toBe(true);
    expect(cookies.join(';')).toContain('cognito_session_deadline=');
  });
});
