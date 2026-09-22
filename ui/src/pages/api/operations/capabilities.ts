import type { NextApiRequest, NextApiResponse } from 'next';
import { OPERATIONS_ROUTES, parseCapabilityDiscovery } from '@/operations/schema';
import { authorizeOperator, upstreamGet, validateAndSend } from '@/operations/proxy';

/**
 * GET /api/operations/capabilities
 * Proxies the frozen E4 route `GET /operations/capabilities`.
 * The UI reads this before showing any operator control.
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }
  const accessToken = await authorizeOperator(req, res);
  if (accessToken === null) return;

  const upstream = await upstreamGet(accessToken, OPERATIONS_ROUTES.capabilities);
  validateAndSend(res, upstream, parseCapabilityDiscovery);
}
