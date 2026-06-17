import type { NextApiRequest, NextApiResponse } from 'next';

export default function handler(req: NextApiRequest, res: NextApiResponse) {
  res.status(200).json({
    cognito: {
      region: process.env.AWS_REGION || process.env.COGNITO_REGION || 'us-west-2',
      userPoolId: process.env.COGNITO_USER_POOL_ID || '',
      clientId: process.env.COGNITO_CLIENT_ID || '',
    },
    agentcore: {
      runtimeId: process.env.AGENTCORE_RUNTIME_ID || '',
    }
  });
}
