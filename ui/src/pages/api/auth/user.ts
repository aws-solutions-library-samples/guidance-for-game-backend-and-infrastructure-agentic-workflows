import type { NextApiRequest, NextApiResponse } from 'next';
import { logError } from '@/utils/logger';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    const idToken = req.cookies.cognito_id_token;

    if (!idToken) {
      return res.status(401).json({ error: 'No authentication token found' });
    }

    // Decode JWT token (no verification needed since it's from our own cookies)
    const payload = JSON.parse(Buffer.from(idToken.split('.')[1], 'base64').toString());

    // Extract user info from token
    const email = payload.email || payload.sub;
    const username = email ? email.split('@')[0] : 'User';

    // Check if user is admin (you can customize this logic)
    const isAdmin = payload['cognito:groups']?.includes('admin') || false;

    return res.status(200).json({
      username,
      email,
      isAdmin,
      sub: payload.sub
    });
  } catch (error) {
    logError('Error parsing user token:', error instanceof Error ? error : new Error(String(error)));
    return res.status(500).json({ error: 'Failed to parse user information' });
  }
}
