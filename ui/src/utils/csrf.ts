/**
 * CSRF defense-in-depth for state-changing API routes.
 *
 * Cookies are already HttpOnly + SameSite=Lax, which neutralizes classic
 * cross-site form/POST CSRF. This adds a second, independent control: a
 * same-origin check on the request's Origin (or Referer) header. The API routes
 * are served from the same host as the frontend, so a legitimate browser
 * request always carries an Origin/Referer whose host matches the request Host.
 * A cross-site forgery either omits Origin (blocked below in prod) or sends a
 * foreign host (blocked).
 *
 * An optional ALLOWED_ORIGINS env var (comma-separated) whitelists additional
 * hosts (e.g. a custom domain in front of the ALB). The explicit local-dev
 * bypass (NEXT_PUBLIC_SKIP_AUTH=true, non-prod) skips the check, mirroring the
 * auth flow — local dev has no fixed public origin.
 */
import type { NextApiRequest } from 'next';

function hostOf(value: string | undefined): string | null {
  if (!value) return null;
  try {
    return new URL(value).host;
  } catch {
    return null;
  }
}

/**
 * Returns true if the request passes the same-origin (CSRF) check.
 *
 * Call this on state-changing requests (POST/PUT/PATCH/DELETE) and reject with
 * 403 when it returns false.
 */
export function isSameOrigin(req: NextApiRequest): boolean {
  // Local-dev bypass: same gate the auth layer uses.
  if (process.env.NODE_ENV !== 'production' && process.env.NEXT_PUBLIC_SKIP_AUTH === 'true') {
    return true;
  }

  const requestHost = req.headers.host; // public host (ALB forwards Host)
  // Prefer Origin; fall back to Referer (some browsers omit Origin on same-origin GETs,
  // but for POST it's reliably present — Referer is a defensive fallback).
  const sourceHost = hostOf(req.headers.origin) ?? hostOf(req.headers.referer);

  // No Origin/Referer at all on a state-changing request: treat as untrusted.
  if (!sourceHost || !requestHost) {
    return false;
  }

  if (sourceHost === requestHost) {
    return true;
  }

  // Optional explicit allowlist (comma-separated origins or bare hosts).
  const allowed = (process.env.ALLOWED_ORIGINS || '')
    .split(',')
    .map((o) => hostOf(o.trim()) ?? o.trim())
    .filter(Boolean);

  return allowed.includes(sourceHost);
}
