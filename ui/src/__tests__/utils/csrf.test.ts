/**
 * Tests for the same-origin (CSRF defense-in-depth) helper.
 */
import type { NextApiRequest } from 'next';
import { isSameOrigin } from '@/utils/csrf';

function reqWith(headers: Record<string, string | undefined>): NextApiRequest {
  return { headers } as unknown as NextApiRequest;
}

describe('isSameOrigin', () => {
  const ORIGINAL_ENV = { ...process.env };

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
  });

  it('allows a matching Origin host', () => {
    process.env.NODE_ENV = 'production';
    expect(isSameOrigin(reqWith({ host: 'app.example.com', origin: 'https://app.example.com' }))).toBe(true);
  });

  it('falls back to Referer when Origin is absent', () => {
    process.env.NODE_ENV = 'production';
    expect(isSameOrigin(reqWith({ host: 'app.example.com', referer: 'https://app.example.com/admin' }))).toBe(true);
  });

  it('blocks a foreign Origin host', () => {
    process.env.NODE_ENV = 'production';
    expect(isSameOrigin(reqWith({ host: 'app.example.com', origin: 'https://evil.example.com' }))).toBe(false);
  });

  it('blocks a request with no Origin or Referer in production', () => {
    process.env.NODE_ENV = 'production';
    expect(isSameOrigin(reqWith({ host: 'app.example.com' }))).toBe(false);
  });

  it('honors the ALLOWED_ORIGINS allowlist', () => {
    process.env.NODE_ENV = 'production';
    process.env.ALLOWED_ORIGINS = 'https://cdn.example.com, https://app.example.com';
    expect(isSameOrigin(reqWith({ host: 'alb-internal.aws', origin: 'https://app.example.com' }))).toBe(true);
  });

  it('bypasses the check for the explicit local-dev flag', () => {
    process.env.NODE_ENV = 'development';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    // No Origin header at all, but dev bypass returns true.
    expect(isSameOrigin(reqWith({ host: 'localhost:3000' }))).toBe(true);
  });

  it('does NOT bypass in a non-prod hosted env without the flag', () => {
    process.env.NODE_ENV = 'development';
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
    expect(isSameOrigin(reqWith({ host: 'staging.example.com', origin: 'https://evil.example.com' }))).toBe(false);
  });
});
