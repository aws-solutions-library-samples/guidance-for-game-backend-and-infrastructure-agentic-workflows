import type { NextApiRequest, NextApiResponse } from 'next';
import { OPERATIONS_ACTION_ROUTES, parseCancelResponse } from '@/operations/schema';
import { isSameOrigin } from '@/utils/csrf';
import { actionPost, authorizeOperator, validateAndSend } from '@/operations/proxy';

const OPERATION_ID_PATTERN = /^op_[a-z0-9]{26}$/;

/**
 * POST /api/operations/[operationId]/cancel
 * Proxies the E2 operations *action* route `POST /operations/{operationId}/cancel`.
 *
 * Cancellation is owned by the E2 approval/decision service, not the E4 control
 * plane, so this route forwards to GBAW_OPERATIONS_ACTION_API_BASE_URL (via
 * actionPost) rather than the control-plane base. That action base has no
 * fallback: if it is missing/malformed, actionPost fails closed with a bounded
 * 502 before any upstream request, so cancellation is never silently misrouted
 * to the control-plane base or a shared/localhost default.
 *
 * Defense in depth on this state-changing request:
 *  - same-origin CSRF check;
 *  - admin-group gate (verified from the ID token) + verified access token
 *    forwarded as the only credential;
 *  - the operation id is validated against the frozen pattern before the backend
 *    is called, so a malformed or path-traversal id never reaches it.
 * The backend re-authorizes (requester or approver) and enforces the cancellable
 * state transition independently; this proxy is not the authorization boundary.
 * The operation id travels only as a path parameter — no identity/credential
 * body is constructed or forwarded.
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  // CSRF defense-in-depth: reject cross-origin state-changing requests.
  if (!isSameOrigin(req)) {
    return res.status(403).json({ error: 'Cross-origin request blocked' });
  }

  const raw = req.query.operationId;
  const operationId = Array.isArray(raw) ? raw[0] : raw;
  if (!operationId || !OPERATION_ID_PATTERN.test(operationId)) {
    return res.status(400).json({ error: 'Invalid operation id' });
  }

  const accessToken = await authorizeOperator(req, res);
  if (accessToken === null) return;

  const path = OPERATIONS_ACTION_ROUTES.cancel.replace(
    '{operationId}',
    encodeURIComponent(operationId),
  );
  // No request body: the operation id is a path parameter and the acting
  // principal is resolved by the backend from the verified caller.
  const upstream = await actionPost(accessToken, path, undefined);
  validateAndSend(res, upstream, parseCancelResponse);
}
