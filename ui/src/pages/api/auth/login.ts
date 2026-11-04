import type { NextApiRequest, NextApiResponse } from 'next';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { isSameOrigin } from '@/utils/csrf';
import { createInitialSessionCookies } from '@/utils/authSession';
import { logError } from '@/utils/logger';

function tokenVerifiers() {
  const userPoolId = process.env.COGNITO_USER_POOL_ID;
  const clientId = process.env.COGNITO_CLIENT_ID;
  if (!userPoolId || !clientId) {
    throw new Error('Cognito authentication is not configured');
  }
  return {
    access: CognitoJwtVerifier.create({ userPoolId, clientId, tokenUse: 'access' }),
    id: CognitoJwtVerifier.create({ userPoolId, clientId, tokenUse: 'id' }),
  };
}

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  if (!isSameOrigin(req)) {
    return res.status(403).json({ error: 'Cross-origin request blocked' });
  }

  const { accessToken, idToken, refreshToken } = req.body ?? {};
  if (
    typeof accessToken !== 'string'
    || typeof idToken !== 'string'
    || typeof refreshToken !== 'string'
    || !accessToken
    || !idToken
    || !refreshToken
  ) {
    return res.status(400).json({ error: 'Complete Cognito session tokens are required' });
  }

  try {
    const verifiers = tokenVerifiers();
    const [accessPayload, idPayload] = await Promise.all([
      verifiers.access.verify(accessToken),
      verifiers.id.verify(idToken),
    ]);
    const cookies = createInitialSessionCookies({
      accessToken,
      idToken,
      refreshToken,
      accessPayload,
      idPayload,
    });
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Set-Cookie', cookies);
    return res.status(200).json({ success: true });
  } catch {
    logError('Cognito login token verification failed');
    return res.status(401).json({ error: 'Invalid authentication tokens' });
  }
}
