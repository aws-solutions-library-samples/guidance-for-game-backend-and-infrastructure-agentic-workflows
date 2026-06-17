import type { NextApiRequest, NextApiResponse } from 'next';
import { serialize } from 'cookie';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  // Clear all auth cookies
  const cookieOptions = {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax' as const,
    path: '/',
    maxAge: 0, // Expire immediately
  };

  const cookies = [
    serialize('cognito_access_token', '', cookieOptions),
    serialize('cognito_id_token', '', cookieOptions),
    serialize('cognito_refresh_token', '', cookieOptions),
  ];

  res.setHeader('Set-Cookie', cookies);
  return res.status(200).json({ success: true });
}
