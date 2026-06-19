import type { NextApiRequest, NextApiResponse } from 'next';
import { CognitoIdentityProviderClient, ListUsersCommand, AdminListGroupsForUserCommand } from '@aws-sdk/client-cognito-identity-provider';
import { parse } from 'cookie';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { logError } from '@/utils/logger';

// Verify the ID token (read from the cognito_id_token cookie below). It must be
// tokenUse: 'id' — aws-jwt-verify checks the token_use claim, so an 'access'
// verifier rejects an ID token outright, breaking admin auth. The ID token also
// reliably carries cognito:groups (used for the admin check).
const verifier = CognitoJwtVerifier.create({
  userPoolId: process.env.COGNITO_USER_POOL_ID!,
  tokenUse: 'id',
  clientId: process.env.COGNITO_CLIENT_ID!,
});

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    // Verify user is admin
    const cookies = parse(req.headers.cookie || '');
    const token = cookies.cognito_id_token;

    if (!token) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const decoded = await verifier.verify(token);
    const groups = (decoded['cognito:groups'] as string[]) || [];

    if (!groups.includes('admin')) {
      return res.status(403).json({ error: 'Admin access required' });
    }

    // List all users
    const client = new CognitoIdentityProviderClient({ region: process.env.AWS_REGION });
    const command = new ListUsersCommand({
      UserPoolId: process.env.COGNITO_USER_POOL_ID
    });

    const response = await client.send(command);

    // Get groups for each user
    const users = await Promise.all((response.Users || []).map(async (user) => {
      const groupsCmd = new AdminListGroupsForUserCommand({
        UserPoolId: process.env.COGNITO_USER_POOL_ID,
        Username: user.Username
      });
      const groupsRes = await client.send(groupsCmd);

      return {
        username: user.Username,
        email: user.Attributes?.find(a => a.Name === 'email')?.Value,
        status: user.UserStatus,
        created: user.UserCreateDate?.toISOString(),
        groups: groupsRes.Groups?.map(g => g.GroupName) || []
      };
    }));

    res.status(200).json({ users });
  } catch (error) {
    logError('Error listing users:', error instanceof Error ? error : new Error(String(error)));
    res.status(500).json({ error: 'Failed to list users' });
  }
}
