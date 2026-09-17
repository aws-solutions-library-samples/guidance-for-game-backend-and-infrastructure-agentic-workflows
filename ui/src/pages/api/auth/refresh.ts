import type { NextApiRequest, NextApiResponse } from 'next';
import {
  CognitoIdentityProviderClient,
  InitiateAuthCommand,
} from '@aws-sdk/client-cognito-identity-provider';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { parse } from '@/utils/cookieCompat';
import { isSameOrigin } from '@/utils/csrf';
import {
  REFRESH_TOKEN_COOKIE,
  SESSION_DEADLINE_COOKIE,
  absoluteRemainingSeconds,
  clearAuthCookies,
  createRefreshedTokenCookies,
} from '@/utils/authSession';
import { logError } from '@/utils/logger';

function configuredClientId(): string {
  const clientId = process.env.COGNITO_CLIENT_ID;
  if (!clientId) throw new Error('Cognito client is not configured');
  return clientId;
}

function tokenVerifiers() {
  const userPoolId = process.env.COGNITO_USER_POOL_ID;
  const clientId = configuredClientId();
  if (!userPoolId) throw new Error('Cognito user pool is not configured');
  return {
    access: CognitoJwtVerifier.create({ userPoolId, clientId, tokenUse: 'access' }),
    id: CognitoJwtVerifier.create({ userPoolId, clientId, tokenUse: 'id' }),
  };
}

function rejectSession(res: NextApiResponse, status = 401) {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Set-Cookie', clearAuthCookies());
  return res.status(status).json({ error: 'Session expired' });
}

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }
  if (!isSameOrigin(req)) {
    return res.status(403).json({ error: 'Cross-origin request blocked' });
  }

  let cookies;
  try {
    cookies = parse(req.headers.cookie || '');
  } catch {
    return rejectSession(res, 400);
  }

  const refreshToken = cookies[REFRESH_TOKEN_COOKIE];
  const remainingSeconds = absoluteRemainingSeconds(cookies[SESSION_DEADLINE_COOKIE]);
  if (!refreshToken || remainingSeconds <= 0) {
    return rejectSession(res);
  }

  try {
    const client = new CognitoIdentityProviderClient({ region: process.env.AWS_REGION });
    const response = await client.send(new InitiateAuthCommand({
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: configuredClientId(),
      AuthParameters: { REFRESH_TOKEN: refreshToken },
    }));
    const accessToken = response.AuthenticationResult?.AccessToken;
    const idToken = response.AuthenticationResult?.IdToken;
    if (!accessToken || !idToken) {
      return rejectSession(res);
    }

    const verifiers = tokenVerifiers();
    const [accessPayload, idPayload] = await Promise.all([
      verifiers.access.verify(accessToken),
      verifiers.id.verify(idToken),
    ]);
    const refreshedCookies = createRefreshedTokenCookies({
      accessToken,
      idToken,
      accessPayload,
      idPayload,
      absoluteRemainingSeconds: remainingSeconds,
    });

    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Set-Cookie', refreshedCookies);
    return res.status(200).json({ success: true });
  } catch {
    logError('Cognito session refresh failed');
    return rejectSession(res);
  }
}
