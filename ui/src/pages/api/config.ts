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
      // Idle-session warning (#310): 30-minute timeout, 2-minute warning window.
      // The client timer is a UX/local-exposure control, not authorization.
      idleTimeoutSeconds: numericConfig(process.env.GBAW_SESSION_IDLE_TIMEOUT_SECONDS, 1800, 300, 28800),
      idleWarningSeconds: numericConfig(process.env.GBAW_SESSION_IDLE_WARNING_SECONDS, 120, 30, 600),
    },
    agentcore: {
      runtimeId: process.env.AGENTCORE_RUNTIME_ID || '',
    },
  });
}
