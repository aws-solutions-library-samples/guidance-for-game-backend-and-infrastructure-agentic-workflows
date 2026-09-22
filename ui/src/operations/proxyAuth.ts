/**
 * Server-side authorization + forwarding helpers for the E4 operations control
 * plane proxy routes (issue #416).
 *
 * Security model (must match the rest of the app's API-proxy pattern):
 *  - The Cognito tokens live in HttpOnly cookies. Browser JS never sees them.
 *  - Every operator proxy route verifies the ID token (for the admin group
 *    claim) AND the access token (the credential forwarded to the backend), and
 *    binds them to the same subject so a valid access token cannot be paired
 *    with a forged id token.
 *  - Only the *verified* access token is forwarded to the backend, as a Bearer
 *    credential in the Authorization header — never the cookie, never the id
 *    token. The backend re-checks authority independently.
 *  - Admin-group enforcement here is defense in depth; a hidden or client-denied
 *    control is never the authorization boundary.
 */
import type { NextApiRequest } from 'next';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { parse } from '@/utils/cookieCompat';
import { ACCESS_TOKEN_COOKIE, ID_TOKEN_COOKIE } from '@/utils/authSession';

const DEFAULT_LOCAL_BACKEND = 'http://localhost:8080';

/**
 * True only for the explicit local-dev bypass. NODE_ENV alone is unsafe (a
 * hosted preview is also !== 'production'), so we require NEXT_PUBLIC_SKIP_AUTH.
 *
 * Declared before the base-URL resolvers because they consult it to decide
 * whether a plaintext (http) backend base is permissible.
 */
export function isLocalDevBypass(): boolean {
  return process.env.NODE_ENV !== 'production' && process.env.NEXT_PUBLIC_SKIP_AUTH === 'true';
}

/**
 * Raised when a server-only base URL is not usable (e.g. plaintext http outside
 * the local-dev bypass). Kept in this module so both the base-URL resolvers and
 * the proxy transport can share one error type.
 */
export class BaseUrlError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BaseUrlError';
  }
}

/**
 * Require the resolved base URL to be transport-secure (HTTPS) unless we are in
 * the explicit local-dev bypass. A misconfigured plaintext base would send the
 * forwarded Bearer access token in the clear, so we fail closed rather than
 * forward the credential. localhost/127.0.0.1 remain acceptable only under the
 * bypass (the local backend has no TLS).
 */
export function requireSecureBase(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new BaseUrlError('operations base URL is not a valid URL');
  }
  if (url.protocol === 'https:') return raw;
  if (url.protocol === 'http:' && isLocalDevBypass()) return raw;
  throw new BaseUrlError('operations base URL must be https outside local-dev');
}

function resolveBase(...candidates: (string | undefined)[]): string {
  const raw = candidates.map((c) => c?.trim()).find((c) => c) || DEFAULT_LOCAL_BACKEND;
  return requireSecureBase(raw.replace(/\/+$/, ''));
}

/**
 * The base URL of the E4 operations control-plane REST API (read + kill-switch
 * control). Server-side only, never exposed to the browser.
 *
 * Resolution order:
 *  1. GBAW_OPERATIONS_API_BASE_URL — the deployed control-plane API stage URL.
 *  2. BACKEND_URL — the shared local-dev backend.
 *  3. http://localhost:8080 — the default local backend.
 *
 * The resolved value must be HTTPS outside the local-dev bypass.
 */
export function operationsBackendBaseUrl(): string {
  return resolveBase(process.env.GBAW_OPERATIONS_API_BASE_URL, process.env.BACKEND_URL);
}

/**
 * The base URL of the E2 operations *action* API (approval-lifecycle decisions
 * such as `POST /operations/{operationId}/cancel`). Cancellation is owned by the
 * E2 approval/decision service, not the E4 control plane, so it has its own
 * server-only base. Server-side only, never exposed to the browser.
 *
 * This base has NO fallback. GBAW_OPERATIONS_ACTION_API_BASE_URL must be
 * explicitly configured; a missing, blank, or malformed value fails closed with
 * a BaseUrlError so cancellation can never be silently misrouted to the E4
 * control-plane base, the shared BACKEND_URL, or a localhost default. The
 * resolved value must be HTTPS outside the local-dev bypass.
 */
export function operationsActionBaseUrl(): string {
  const raw = process.env.GBAW_OPERATIONS_ACTION_API_BASE_URL?.trim();
  if (!raw) {
    throw new BaseUrlError(
      'GBAW_OPERATIONS_ACTION_API_BASE_URL must be explicitly configured for cancellation',
    );
  }
  return requireSecureBase(raw.replace(/\/+$/, ''));
}

/** Headers for a forwarded backend request. The access token is the only credential. */
export function forwardHeaders(accessToken: string): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };
}

export class OperatorAuthError extends Error {
  readonly status: number;
  readonly publicMessage: string;
  constructor(status: number, publicMessage: string) {
    super(publicMessage);
    this.name = 'OperatorAuthError';
    this.status = status;
    this.publicMessage = publicMessage;
  }
}

interface TokenVerifier {
  verify(token: string): Promise<Record<string, unknown>>;
}

interface VerifierBundle {
  idVerifier: TokenVerifier;
  accessVerifier: TokenVerifier;
}

export interface OperatorAdmin {
  accessToken: string;
  subject: string;
  isAdmin: true;
}

// Lazy singletons for production (real Cognito verifiers). Tests inject stubs.
let idVerifierSingleton: TokenVerifier | null = null;
let accessVerifierSingleton: TokenVerifier | null = null;

function productionVerifiers(): VerifierBundle {
  if (!idVerifierSingleton) {
    idVerifierSingleton = CognitoJwtVerifier.create({
      userPoolId: process.env.COGNITO_USER_POOL_ID!,
      tokenUse: 'id',
      clientId: process.env.COGNITO_CLIENT_ID!,
    });
  }
  if (!accessVerifierSingleton) {
    accessVerifierSingleton = CognitoJwtVerifier.create({
      userPoolId: process.env.COGNITO_USER_POOL_ID!,
      tokenUse: 'access',
      clientId: process.env.COGNITO_CLIENT_ID!,
    });
  }
  return { idVerifier: idVerifierSingleton, accessVerifier: accessVerifierSingleton };
}

/**
 * Verify the caller, require the admin group, and return the verified access
 * token to forward. Throws OperatorAuthError (401/403) on any failure.
 *
 * In the local-dev bypass, returns a synthetic admin with no token (the local
 * backend does not require a Bearer credential).
 *
 * `verifiers` is resolved lazily *after* the bypass check so unit tests and
 * local dev never construct real Cognito verifiers (which require pool/client
 * env vars). Production callers omit it and get the cached real verifiers.
 */
export async function requireOperatorAdmin(
  req: NextApiRequest,
  verifiers?: VerifierBundle,
): Promise<OperatorAdmin> {
  if (isLocalDevBypass()) {
    return { accessToken: '', subject: 'dev-user', isAdmin: true };
  }

  const resolved = verifiers ?? productionVerifiers();

  let cookies: Record<string, string | undefined>;
  try {
    cookies = parse(req.headers.cookie || '');
  } catch {
    throw new OperatorAuthError(400, 'Invalid cookie format');
  }

  const idToken = cookies[ID_TOKEN_COOKIE];
  const accessToken = cookies[ACCESS_TOKEN_COOKIE];
  if (!idToken || !accessToken) {
    throw new OperatorAuthError(401, 'Authentication required');
  }

  let idClaims: Record<string, unknown>;
  let accessClaims: Record<string, unknown>;
  try {
    idClaims = await resolved.idVerifier.verify(idToken);
  } catch {
    throw new OperatorAuthError(401, 'Invalid or expired session');
  }
  try {
    accessClaims = await resolved.accessVerifier.verify(accessToken);
  } catch {
    throw new OperatorAuthError(401, 'Invalid or expired session');
  }

  const idSub = typeof idClaims.sub === 'string' ? idClaims.sub : undefined;
  const accessSub = typeof accessClaims.sub === 'string' ? accessClaims.sub : undefined;
  if (!idSub || !accessSub || idSub !== accessSub) {
    throw new OperatorAuthError(401, 'Session token mismatch');
  }

  const groups = Array.isArray(idClaims['cognito:groups'])
    ? (idClaims['cognito:groups'] as unknown[]).filter((g): g is string => typeof g === 'string')
    : [];
  if (!groups.includes('admin')) {
    throw new OperatorAuthError(403, 'Operator access requires the admin group');
  }

  return { accessToken, subject: idSub, isAdmin: true };
}
