import type { NextApiRequest, NextApiResponse } from 'next';
import { CognitoIdentityProviderClient, AdminConfirmSignUpCommand, AdminAddUserToGroupCommand, AdminDeleteUserCommand } from '@aws-sdk/client-cognito-identity-provider';
import { parse } from 'cookie';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { logError } from '@/utils/logger';

const verifier = CognitoJwtVerifier.create({
  userPoolId: process.env.COGNITO_USER_POOL_ID!,
  tokenUse: 'access',
  clientId: process.env.COGNITO_CLIENT_ID!,
});

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    // Verify user is admin
    let cookies;
    try {
      cookies = parse(req.headers.cookie || '');
    } catch {
      return res.status(400).json({ error: 'Invalid cookie format' });
    }
    const token = cookies.cognito_id_token;

    if (!token) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    let decoded;
    try {
      decoded = await verifier.verify(token);
    } catch (verifyError) {
      // Handle JWT verification errors
      if (verifyError instanceof Error) {
        if (verifyError.name === 'JwtExpiredError' || verifyError.name === 'JwtInvalidSignatureError') {
          return res.status(401).json({ error: 'Invalid or expired token' });
        }
        if (verifyError.name === 'JwtParseError') {
          return res.status(400).json({ error: 'Malformed token' });
        }
      }
      throw verifyError;
    }

    const groups = (decoded['cognito:groups'] as string[]) || [];

    if (!groups.includes('admin')) {
      return res.status(403).json({ error: 'Admin access required' });
    }

    const { username, action } = req.body;
    const client = new CognitoIdentityProviderClient({ region: process.env.AWS_REGION });

    if (action === 'approve') {
      // Confirm user
      await client.send(new AdminConfirmSignUpCommand({
        UserPoolId: process.env.COGNITO_USER_POOL_ID,
        Username: username
      }));

      // Add to users group
      await client.send(new AdminAddUserToGroupCommand({
        UserPoolId: process.env.COGNITO_USER_POOL_ID,
        Username: username,
        GroupName: 'users'
      }));

      res.status(200).json({ message: 'User approved' });
    } else if (action === 'deny') {
      // Delete user
      await client.send(new AdminDeleteUserCommand({
        UserPoolId: process.env.COGNITO_USER_POOL_ID,
        Username: username
      }));

      res.status(200).json({ message: 'User denied and deleted' });
    } else {
      res.status(400).json({ error: 'Invalid action' });
    }
  } catch (error) {
    // Handle AWS SDK errors
    if (error && typeof error === 'object' && '$metadata' in error) {
      logError('AWS Cognito error:', error instanceof Error ? error : new Error(String(error)));
      return res.status(503).json({ error: 'Service temporarily unavailable' });
    }

    // Generic fallback
    logError('Error processing user action:', error instanceof Error ? error : new Error(String(error)));
    res.status(500).json({ error: 'Failed to process action' });
  }
}
