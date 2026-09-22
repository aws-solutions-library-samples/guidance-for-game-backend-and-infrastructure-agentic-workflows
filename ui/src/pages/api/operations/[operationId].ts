import type { NextApiRequest, NextApiResponse } from 'next';
import { OPERATIONS_ROUTES, parseOperationDetail } from '@/operations/schema';
import { authorizeOperator, upstreamGet, validateAndSend } from '@/operations/proxy';

const OPERATION_ID_PATTERN = /^op_[a-z0-9]{26}$/;

/**
 * GET /api/operations/[operationId]
 * Proxies the frozen E4 route `GET /operations/{operationId}`.
 *
 * The path segment is validated against the frozen operation-id pattern before
 * the backend is called, so a malformed or path-traversal id never reaches it.
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const raw = req.query.operationId;
  const operationId = Array.isArray(raw) ? raw[0] : raw;
  if (!operationId || !OPERATION_ID_PATTERN.test(operationId)) {
    return res.status(400).json({ error: 'Invalid operation id' });
  }

  const accessToken = await authorizeOperator(req, res);
  if (accessToken === null) return;

  const path = OPERATIONS_ROUTES.detail.replace('{operationId}', encodeURIComponent(operationId));
  const upstream = await upstreamGet(accessToken, path);
  validateAndSend(res, upstream, parseOperationDetail);
}
