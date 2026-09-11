import type { NextApiRequest, NextApiResponse } from 'next';
import { clearAuthCookies } from '@/utils/authSession';
import { isSameOrigin } from '@/utils/csrf';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  if (!isSameOrigin(req)) {
    return res.status(403).json({ error: 'Cross-origin request blocked' });
  }

  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Set-Cookie', clearAuthCookies());
  return res.status(200).json({ success: true });
}
