import type { NextApiRequest, NextApiResponse } from 'next';
import {
  OPERATIONS_ROUTES,
  parseControlRequest,
  parseControlResponse,
  SchemaGuardError,
} from '@/operations/schema';
import { isSameOrigin } from '@/utils/csrf';
import { authorizeOperator, upstreamPost, validateAndSend } from '@/operations/proxy';

/**
 * POST /api/operations/control
 * Proxies the frozen E4 route `POST /operations/control`.
 *
 * This is the deployment/per-capability kill-switch control. Defense in depth:
 *  - same-origin CSRF check (state-changing request);
 *  - admin-group gate (verified from the ID token);
 *  - strict validation of the request body against the frozen control-request
 *    shape, rejecting any identity/credential/policy field before forwarding.
 * Only the frozen, identity-free body is forwarded; the backend resolves the
 * acting admin from the verified caller and enforces authority independently.
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  // CSRF defense-in-depth: reject cross-origin state-changing requests.
  if (!isSameOrigin(req)) {
    return res.status(403).json({ error: 'Cross-origin request blocked' });
  }

  // Validate the request body against the frozen control-request contract.
  let controlRequest;
  try {
    const parsedBody =
      typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body ?? {});
    controlRequest = parseControlRequest(parsedBody);
  } catch (error) {
    if (error instanceof SchemaGuardError) {
      return res.status(400).json({ error: 'Invalid control request' });
    }
    return res.status(400).json({ error: 'Malformed request body' });
  }

  const accessToken = await authorizeOperator(req, res);
  if (accessToken === null) return;

  const upstream = await upstreamPost(accessToken, OPERATIONS_ROUTES.control, controlRequest);
  validateAndSend(res, upstream, parseControlResponse);
}
