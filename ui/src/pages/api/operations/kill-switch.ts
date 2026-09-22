import type { NextApiRequest, NextApiResponse } from 'next';
import { OPERATIONS_ROUTES, parseKillSwitch } from '@/operations/schema';
import { authorizeOperator, upstreamGet, validateAndSend } from '@/operations/proxy';

/**
 * GET /api/operations/kill-switch
 * Proxies the frozen E4 route `GET /operations/control/kill-switch`.
 * Returns the current kill-switch document (with its config_version) so the UI
 * can render gate state and supply expected_config_version for a compare-and-set.
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }
  const accessToken = await authorizeOperator(req, res);
  if (accessToken === null) return;

  const upstream = await upstreamGet(accessToken, OPERATIONS_ROUTES.killSwitch);
  validateAndSend(res, upstream, parseKillSwitch);
}
