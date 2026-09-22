/**
 * Shared server-side proxy plumbing for the E4 operations control-plane routes.
 *
 * Each Next.js API route stays thin: it verifies the operator (admin group),
 * forwards the verified access token as a Bearer credential to the frozen E4
 * backend route, then validates the backend's response with the schema guards
 * before returning it. If validation fails we fail closed with a 502 and never
 * echo the raw upstream body — the UI is never the thing that leaks an
 * out-of-contract or sensitive payload.
 *
 * Transport hardening (defense in depth, below the schema guards):
 *  - fetch is issued with redirect:'manual'. A 3xx upstream is treated as a
 *    contract violation and mapped to a bounded 502 — the forwarded Bearer
 *    credential is never replayed to a redirect target the upstream chose.
 *  - the server-only base URL must be HTTPS outside the explicit local-dev
 *    bypass, so the credential is never sent in the clear.
 *  - the response body is read under a hard byte ceiling (Content-Length and
 *    streamed) *before* JSON parsing, so a hostile/huge body cannot exhaust
 *    memory; an oversize or malformed body maps to a bounded 502.
 */
import type { NextApiRequest, NextApiResponse } from 'next';
import { logError } from '@/utils/logger';
import { SchemaGuardError } from '@/operations/schema';
import {
  BaseUrlError,
  OperatorAuthError,
  forwardHeaders,
  operationsActionBaseUrl,
  operationsBackendBaseUrl,
  requireOperatorAdmin,
  requireSecureBase,
} from '@/operations/proxyAuth';

const PROXY_TIMEOUT_MS = 15_000;

/**
 * Hard ceiling on an upstream response body. The E4 projections are small
 * (bounded page sizes, bounded evidence/phase arrays); 512 KiB is generous
 * headroom while still refusing a pathological body before it is buffered.
 */
export const MAX_UPSTREAM_BYTES = 512 * 1024;

export interface UpstreamResult {
  status: number;
  body: unknown;
}

/**
 * Re-exported so routes and tests share one HTTPS-enforcement helper. Throws
 * BaseUrlError (from proxyAuth) on an insecure base outside the local-dev bypass.
 */
export function requireSecureBaseUrl(raw: string): string {
  return requireSecureBase(raw);
}

/**
 * Raised for any transport-boundary failure (redirect, oversize, malformed
 * body, network abort). Callers map it to a bounded 502; it never carries
 * upstream payload detail.
 */
export class UpstreamProxyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UpstreamProxyError';
  }
}

/**
 * Read a response body under a hard byte ceiling and parse it as JSON.
 *
 * Order matters: we reject on a declared Content-Length over the ceiling before
 * reading a single byte, then enforce the same ceiling while draining the
 * stream (a lying or absent Content-Length cannot get past it). Only then do we
 * parse. Any violation raises UpstreamProxyError so no partial/huge/garbage body
 * ever reaches the schema guards.
 */
export async function readBoundedJson(resp: Response): Promise<unknown> {
  const declared = resp.headers.get('content-length');
  if (declared !== null) {
    const n = Number(declared);
    if (Number.isFinite(n) && n > MAX_UPSTREAM_BYTES) {
      throw new UpstreamProxyError('upstream response exceeds byte ceiling (Content-Length)');
    }
  }

  const reader = (resp.body as ReadableStream<Uint8Array> | null)?.getReader?.();
  let text: string;
  if (reader) {
    const chunks: Uint8Array[] = [];
    let total = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) {
          total += value.byteLength;
          if (total > MAX_UPSTREAM_BYTES) {
            void reader.cancel?.();
            throw new UpstreamProxyError('upstream response exceeds byte ceiling (streamed)');
          }
          chunks.push(value);
        }
      }
    } finally {
      reader.releaseLock?.();
    }
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const c of chunks) {
      merged.set(c, offset);
      offset += c.byteLength;
    }
    text = new TextDecoder().decode(merged);
  } else if (typeof (resp as { text?: () => Promise<string> }).text === 'function') {
    // Fallback for environments without a streamable body: bounded by length.
    text = await (resp as { text: () => Promise<string> }).text();
    if (text.length > MAX_UPSTREAM_BYTES) {
      throw new UpstreamProxyError('upstream response exceeds byte ceiling (text)');
    }
  } else {
    throw new UpstreamProxyError('upstream response has no readable body');
  }

  if (text.trim() === '') return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new UpstreamProxyError('upstream response is not valid JSON');
  }
}

/**
 * Perform an authenticated request against a frozen backend path under all the
 * transport-hardening controls and return a normalized result. A transport
 * failure — including an insecure (non-HTTPS) base URL — is normalized to
 * status 502 (with an empty body) so callers never have to distinguish
 * transport faults from an upstream contract violation; both fail closed
 * identically and the forwarded credential is never sent in the clear or
 * replayed to a redirect target.
 *
 * `resolveBase` is a thunk resolved *inside* the try so a base-URL rejection
 * (BaseUrlError) is caught here and mapped to 502 rather than thrown to the
 * route handler.
 */
async function upstreamFetch(
  resolveBase: () => string,
  accessToken: string,
  path: string,
  init: { method: 'GET' | 'POST'; body?: string },
): Promise<UpstreamResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROXY_TIMEOUT_MS);
  try {
    const secureBase = resolveBase();
    const resp = await fetch(`${secureBase}${path}`, {
      method: init.method,
      headers: forwardHeaders(accessToken),
      body: init.body,
      redirect: 'manual',
      signal: controller.signal,
    });

    // A manual-redirect fetch surfaces 3xx as a real status (or an opaqueredirect
    // status of 0). Either way the upstream tried to redirect our credentialed
    // request; we refuse to follow and fail closed.
    if (resp.status === 0 || (resp.status >= 300 && resp.status < 400)) {
      logError(`Operations upstream returned a redirect (${resp.status}); refusing to follow`);
      return { status: 502, body: {} };
    }

    const body = await readBoundedJson(resp);
    return { status: resp.status, body };
  } catch (error) {
    if (error instanceof BaseUrlError) {
      logError(`Operations proxy refused an insecure base URL: ${error.message}`);
      return { status: 502, body: {} };
    }
    if (error instanceof UpstreamProxyError) {
      logError(`Operations upstream transport failure: ${error.message}`);
      return { status: 502, body: {} };
    }
    logError('Operations upstream request failed', error instanceof Error ? error : undefined);
    return { status: 502, body: {} };
  } finally {
    clearTimeout(timer);
  }
}

/** Authenticated GET against the E4 control-plane base. */
export async function upstreamGet(accessToken: string, path: string): Promise<UpstreamResult> {
  return upstreamFetch(operationsBackendBaseUrl, accessToken, path, { method: 'GET' });
}

/** Authenticated POST of a JSON body against the E4 control-plane base. */
export async function upstreamPost(
  accessToken: string,
  path: string,
  requestBody: unknown,
): Promise<UpstreamResult> {
  return upstreamFetch(operationsBackendBaseUrl, accessToken, path, {
    method: 'POST',
    body: JSON.stringify(requestBody),
  });
}

/** Authenticated POST of a JSON body against the E2 operations *action* base. */
export async function actionPost(
  accessToken: string,
  path: string,
  requestBody: unknown,
): Promise<UpstreamResult> {
  return upstreamFetch(operationsActionBaseUrl, accessToken, path, {
    method: 'POST',
    body: JSON.stringify(requestBody),
  });
}

/**
 * Convert an upstream (non-2xx) result into a client-safe status. We forward the
 * status class (401/403/404/409/etc.) but replace the body with a generic
 * message so no upstream detail leaks.
 */
export function sendUpstreamError(res: NextApiResponse, status: number): void {
  const safeStatus = status >= 400 && status < 600 ? status : 502;
  res.status(safeStatus).json({ error: 'The operations service rejected the request' });
}

/**
 * Run the admin gate and translate an OperatorAuthError into an HTTP response.
 * Returns the verified access token on success, or null (response already sent)
 * on failure.
 */
export async function authorizeOperator(
  req: NextApiRequest,
  res: NextApiResponse,
): Promise<string | null> {
  try {
    const admin = await requireOperatorAdmin(req);
    return admin.accessToken;
  } catch (error) {
    if (error instanceof OperatorAuthError) {
      res.status(error.status).json({ error: error.publicMessage });
      return null;
    }
    logError('Operator authorization failed', error instanceof Error ? error : undefined);
    res.status(500).json({ error: 'Authorization error' });
    return null;
  }
}

/**
 * Validate an upstream 2xx body with a schema guard and send it, or fail closed.
 * Non-2xx upstream results are surfaced as a sanitized error.
 */
export function validateAndSend<T>(
  res: NextApiResponse,
  upstream: UpstreamResult,
  guard: (value: unknown) => T,
): void {
  if (upstream.status < 200 || upstream.status >= 300) {
    sendUpstreamError(res, upstream.status);
    return;
  }
  try {
    const validated = guard(upstream.body);
    res.status(200).json(validated);
  } catch (error) {
    if (error instanceof SchemaGuardError) {
      // Log the shape violation (no payload) and fail closed.
      logError(`Operations upstream failed contract validation: ${error.message}`);
      res.status(502).json({ error: 'The operations service returned an invalid response' });
      return;
    }
    logError('Operations proxy error', error instanceof Error ? error : undefined);
    res.status(502).json({ error: 'The operations service returned an invalid response' });
  }
}
