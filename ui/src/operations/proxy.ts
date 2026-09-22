/**
 * Shared server-side proxy plumbing for the E4 operations control-plane routes.
 *
 * Each Next.js API route stays thin: it verifies the operator (admin group),
 * forwards the verified access token as a Bearer credential to the frozen E4
 * backend route, then validates the backend's response with the schema guards
 * before returning it. If validation fails we fail closed with a 502 and never
 * echo the raw upstream body — the UI is never the thing that leaks an
 * out-of-contract or sensitive payload.
 */
import type { NextApiRequest, NextApiResponse } from 'next';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import { logError } from '@/utils/logger';
import { SchemaGuardError } from '@/operations/schema';
import {
  OperatorAuthError,
  forwardHeaders,
  operationsBackendBaseUrl,
  requireOperatorAdmin,
} from '@/operations/proxyAuth';

const PROXY_TIMEOUT_MS = 15_000;

export interface UpstreamResult {
  status: number;
  body: unknown;
}

/** Perform an authenticated GET against a frozen backend path and return the raw result. */
export async function upstreamGet(accessToken: string, path: string): Promise<UpstreamResult> {
  const url = `${operationsBackendBaseUrl()}${path}`;
  const resp = await fetchWithTimeout(
    url,
    { method: 'GET', headers: forwardHeaders(accessToken) },
    PROXY_TIMEOUT_MS,
  );
  const body = await resp.json().catch(() => ({}));
  return { status: resp.status, body };
}

/** Perform an authenticated POST of a JSON body against a frozen backend path. */
export async function upstreamPost(
  accessToken: string,
  path: string,
  requestBody: unknown,
): Promise<UpstreamResult> {
  const url = `${operationsBackendBaseUrl()}${path}`;
  const resp = await fetchWithTimeout(
    url,
    { method: 'POST', headers: forwardHeaders(accessToken), body: JSON.stringify(requestBody) },
    PROXY_TIMEOUT_MS,
  );
  const body = await resp.json().catch(() => ({}));
  return { status: resp.status, body };
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
