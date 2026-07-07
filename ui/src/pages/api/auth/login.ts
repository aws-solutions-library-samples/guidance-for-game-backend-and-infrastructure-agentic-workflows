import type { NextApiRequest, NextApiResponse } from 'next';
import { serialize } from '@/utils/cookieCompat';
import { isSameOrigin } from '@/utils/csrf';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  // CSRF defense-in-depth: reject cross-origin state-changing requests.
  if (!isSameOrigin(req)) {
    return res.status(403).json({ error: 'Cross-origin request blocked' });
  }

  const { accessToken, idToken, refreshToken } = req.body;

  if (!accessToken) {
    return res.status(400).json({ error: 'Access token required' });
  }

  // Set HttpOnly cookies
  const isProduction = process.env.NODE_ENV === 'production';

  const cookieOptions = {
    httpOnly: true,
    secure: isProduction,
    sameSite: 'lax' as const,
    path: '/',
    maxAge: 60 * 60, // 1 hour (matches Cognito access token expiration)
  };

  const cookies = [
    serialize('cognito_access_token', accessToken, cookieOptions),
  ];

  if (idToken) {
    cookies.push(serialize('cognito_id_token', idToken, cookieOptions));
  }

  if (refreshToken) {
    cookies.push(serialize('cognito_refresh_token', refreshToken, {
      ...cookieOptions,
      maxAge: 60 * 60 * 24 * 30, // 30 days
    }));
  }

  res.setHeader('Set-Cookie', cookies);
  return res.status(200).json({ success: true });
}
