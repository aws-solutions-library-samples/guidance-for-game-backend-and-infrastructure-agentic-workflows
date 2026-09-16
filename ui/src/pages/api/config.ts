import type { NextApiRequest, NextApiResponse } from 'next';

function numericConfig(value: string | undefined, fallback: number, minimum: number, maximum: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
  return Math.min(maximum, Math.max(minimum, parsed));
}

export default function handler(req: NextApiRequest, res: NextApiResponse) {
  res.status(200).json({
    cognito: {
      region: process.env.AWS_REGION || process.env.COGNITO_REGION || 'us-west-2',
      userPoolId: process.env.COGNITO_USER_POOL_ID || '',
      clientId: process.env.COGNITO_CLIENT_ID || '',
    },
    session: {
      absoluteLifetimeHours: numericConfig(process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS, 8, 1, 24),
      idleRefreshSeconds: numericConfig(process.env.GBAW_SESSION_IDLE_REFRESH_SECONDS, 900, 60, 3600),
    },
    agentcore: {
      runtimeId: process.env.AGENTCORE_RUNTIME_ID || '',
    },
  });
}
