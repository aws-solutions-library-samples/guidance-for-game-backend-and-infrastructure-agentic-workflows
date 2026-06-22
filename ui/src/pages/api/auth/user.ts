import type { NextApiRequest, NextApiResponse } from 'next';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { logError } from '@/utils/logger';

// Cryptographically verify the ID token (signature + token_use + expiry) rather
// than raw-decoding it. The cookie is HttpOnly so the browser can't tamper with
// it, but a stolen/forged token presented directly to this endpoint must not be
// trusted — its claims (email, cognito:groups → isAdmin) drive what the UI shows.
// tokenUse: 'id' because aws-jwt-verify enforces the token_use claim, and the ID
// token is the one that reliably carries cognito:groups. Matches /api/admin/*.
const verifier = CognitoJwtVerifier.create({
  userPoolId: process.env.COGNITO_USER_POOL_ID!,
  tokenUse: 'id',
  clientId: process.env.COGNITO_CLIENT_ID!,
});

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const idToken = req.cookies.cognito_id_token;

  if (!idToken) {
    return res.status(401).json({ error: 'No authentication token found' });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let payload: any;
  try {
    payload = await verifier.verify(idToken);
  } catch (error) {
    // A token that fails verification is an auth failure (401), not a 500.
    logError('ID token verification failed:', error instanceof Error ? error : new Error(String(error)));
    return res.status(401).json({ error: 'Invalid authentication token' });
  }

  // Extract user info from the verified claims.
  const email = payload.email || payload.sub;
  const username = email ? email.split('@')[0] : 'User';
  const isAdmin = (payload['cognito:groups'] as string[])?.includes('admin') || false;

  return res.status(200).json({
    username,
    email,
    isAdmin,
    sub: payload.sub
  });
}
