import { useState } from 'react';
import { CognitoUserPool, CognitoUser, AuthenticationDetails, CognitoUserSession } from 'amazon-cognito-identity-js';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import ThemeToggle from './ThemeToggle';

interface CognitoAuthProps {
  userPoolId: string;
  clientId: string;
  onAuthenticated: (user: CognitoUser, session: CognitoUserSession) => void;
}

// Self-signup is intentionally unsupported (admin-create-only pool, #154):
// users are provisioned via script. Only sign-in + password reset are exposed.
type AuthMode = 'signin' | 'forgot' | 'reset';

export default function CognitoAuth({ userPoolId, clientId, onAuthenticated }: CognitoAuthProps) {
  const [mode, setMode] = useState<AuthMode>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [verificationCode, setVerificationCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const userPool = new CognitoUserPool({
    UserPoolId: userPoolId,
    ClientId: clientId,
  });

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    const user = new CognitoUser({
      Username: email,
      Pool: userPool,
    });

    const authDetails = new AuthenticationDetails({
      Username: email,
      Password: password,
    });

    user.authenticateUser(authDetails, {
      onSuccess: async (session) => {
        try {
          // Store tokens in HttpOnly cookies via API
          const response = await fetchWithTimeout('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              accessToken: session.getAccessToken().getJwtToken(),
              idToken: session.getIdToken().getJwtToken(),
              refreshToken: session.getRefreshToken().getToken(),
            }),
          });

          if (!response.ok) {
            throw new Error('Failed to store authentication tokens');
          }

          onAuthenticated(user, session);
        } catch {
          setError('Authentication succeeded but failed to store session. Please try again.');
          setLoading(false);
        }
      },
      onFailure: (err) => {
        setError(err.message || 'Authentication failed');
        setLoading(false);
      },
    });
  };

  const handleForgotPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    const user = new CognitoUser({
      Username: email,
      Pool: userPool,
    });

    user.forgotPassword({
      onSuccess: () => {
        setMessage('Reset code sent to your email');
        setMode('reset');
        setLoading(false);
      },
      onFailure: (err) => {
        setError(err.message || 'Failed to send reset code');
        setLoading(false);
      },
    });
  };

  const handleResetPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match');
      setLoading(false);
      return;
    }

    const user = new CognitoUser({
      Username: email,
      Pool: userPool,
    });

    user.confirmPassword(verificationCode, password, {
      onSuccess: () => {
        setMessage('Password reset successful! You can now sign in.');
        setMode('signin');
        setVerificationCode('');
        setPassword('');
        setConfirmPassword('');
        setLoading(false);
      },
      onFailure: (err) => {
        setError(err.message || 'Password reset failed');
        setLoading(false);
      },
    });
  };

  return (
    <div className="auth-container">
      <div className="auth-theme-toggle">
        <ThemeToggle />
      </div>
      <div className="auth-card">
        <div className="auth-header">
          <div className="auth-logo">🛡️</div>
          <h1>Game Agent</h1>
          <p>AI-Powered Game Server Management</p>
        </div>

        {mode === 'signin' && (
          <>
            {/* Self-signup is intentionally not offered: the Cognito user pool is
                admin-create-only (AdminCreateUserConfig.AllowAdminCreateUserOnly),
                so userPool.signUp() always fails. Accounts are provisioned by an
                administrator. (#154) */}
            <div className="auth-tabs">
              <button className="active">Sign In</button>
            </div>

            <form onSubmit={handleSignIn}>
              <div className="form-group">
                <label>Email</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  required
                />
              </div>

              <div className="form-group">
                <label>Password</label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                />
              </div>

              {error && <div className="auth-error">{error}</div>}
              {message && <div className="auth-success">{message}</div>}

              <button type="submit" className="auth-submit" disabled={loading}>
                {loading ? 'Signing in...' : 'Sign In'}
              </button>

              <button type="button" className="auth-link" onClick={() => setMode('forgot')}>
                Forgot password?
              </button>

              <p className="auth-note">Need access? Contact your administrator to be invited.</p>
            </form>
          </>
        )}

        {mode === 'forgot' && (
          <>
            <div className="auth-back">
              <button onClick={() => setMode('signin')}>← Back to Sign In</button>
            </div>

            <h2>Reset Password</h2>
            <p className="auth-subtitle">Enter your email to receive a reset code</p>

            <form onSubmit={handleForgotPassword}>
              <div className="form-group">
                <label>Email</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  required
                />
              </div>

              {error && <div className="auth-error">{error}</div>}
              {message && <div className="auth-success">{message}</div>}

              <button type="submit" className="auth-submit" disabled={loading}>
                {loading ? 'Sending...' : 'Send Reset Code'}
              </button>
            </form>
          </>
        )}

        {mode === 'reset' && (
          <>
            <div className="auth-back">
              <button onClick={() => setMode('signin')}>← Back to Sign In</button>
            </div>

            <h2>Reset Password</h2>
            <p className="auth-subtitle">Enter the code from your email and new password</p>

            <form onSubmit={handleResetPassword}>
              <div className="form-group">
                <label>Reset Code</label>
                <input
                  type="text"
                  value={verificationCode}
                  onChange={(e) => setVerificationCode(e.target.value)}
                  placeholder="123456"
                  required
                />
              </div>

              <div className="form-group">
                <label>New Password</label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                />
              </div>

              <div className="form-group">
                <label>Confirm New Password</label>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                />
              </div>

              {error && <div className="auth-error">{error}</div>}
              {message && <div className="auth-success">{message}</div>}

              <button type="submit" className="auth-submit" disabled={loading}>
                {loading ? 'Resetting...' : 'Reset Password'}
              </button>
            </form>
          </>
        )}
      </div>

      <style jsx>{`
        .auth-container {
          min-height: 100vh;
          display: flex;
          align-items: center;
          justify-content: center;
          background: var(--ga-bg-gradient);
          padding: 20px;
          position: relative;
          overflow: hidden;
        }

        .auth-theme-toggle {
          position: fixed;
          top: 20px;
          right: 20px;
          z-index: 2;
        }

        .auth-container::before {
          content: '';
          position: absolute;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: var(--ga-scan-gradient);
          animation: scan 4s infinite;
        }

        @keyframes scan {
          0%, 100% { transform: translateX(-100%); }
          50% { transform: translateX(100%); }
        }

        .auth-card {
          background: var(--ga-surface-strong);
          border: 1px solid var(--ga-accent-border);
          border-radius: 16px;
          padding: 48px;
          width: 100%;
          max-width: 440px;
          box-shadow: var(--ga-shadow);
          backdrop-filter: blur(10px);
          position: relative;
          z-index: 1;
        }

        .auth-header {
          text-align: center;
          margin-bottom: 32px;
        }

        .auth-logo {
          font-size: 64px;
          margin-bottom: 16px;
        }

        .auth-header h1 {
          font-size: 28px;
          font-weight: 700;
          margin: 0 0 8px 0;
          background: var(--ga-auth-action-gradient);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          background-clip: text;
        }

        .auth-header p {
          color: var(--ga-text-subtle);
          margin: 0;
          font-size: 14px;
        }

        h2 {
          font-size: 24px;
          margin: 0 0 8px 0;
          color: var(--ga-text);
        }

        .auth-subtitle {
          color: var(--ga-text-subtle);
          margin: 0 0 24px 0;
          font-size: 14px;
        }

        .auth-tabs {
          display: flex;
          gap: 8px;
          margin-bottom: 32px;
          background: var(--ga-surface-muted);
          padding: 4px;
          border-radius: 8px;
        }

        .auth-tabs button {
          flex: 1;
          padding: 12px;
          border-radius: 6px;
          font-weight: 500;
          transition: all 0.2s;
          color: var(--ga-text-subtle);
        }

        .auth-tabs button.active {
          background: var(--ga-auth-action-gradient);
          color: #ffffff;
        }

        .auth-back {
          margin-bottom: 24px;
        }

        .auth-back button {
          color: var(--ga-text-subtle);
          font-size: 14px;
          padding: 8px 0;
          transition: color 0.2s;
        }

        .auth-back button:hover {
          color: var(--ga-accent);
        }

        .form-group {
          margin-bottom: 20px;
        }

        .form-group label {
          display: block;
          margin-bottom: 8px;
          font-weight: 500;
          font-size: 14px;
          color: var(--ga-text);
        }

        .form-group input {
          width: 100%;
          padding: 12px 16px;
          background: var(--ga-input-bg);
          border: 1px solid var(--ga-accent-border);
          border-radius: 8px;
          color: var(--ga-text);
          font-size: 14px;
          transition: all 0.2s;
        }

        .form-group input:focus {
          outline: none;
          border-color: var(--ga-accent);
          box-shadow: 0 0 0 3px rgba(139, 69, 255, 0.1);
        }

        .form-group input::placeholder {
          color: var(--ga-text-faint);
        }

        .auth-error {
          padding: 12px;
          background: var(--ga-danger-bg);
          border: 1px solid var(--ga-danger-border);
          border-radius: 8px;
          color: var(--ga-danger);
          font-size: 14px;
          margin-bottom: 20px;
        }

        .auth-success {
          padding: 12px;
          background: var(--ga-success-bg);
          border: 1px solid var(--ga-success-border);
          border-radius: 8px;
          color: var(--ga-success);
          font-size: 14px;
          margin-bottom: 20px;
        }

        .auth-submit {
          width: 100%;
          padding: 14px;
          background: var(--ga-auth-action-gradient);
          color: #ffffff;
          font-weight: 600;
          font-size: 16px;
          border-radius: 8px;
          transition: all 0.2s;
          margin-bottom: 16px;
        }

        .auth-submit:hover:not(:disabled) {
          transform: translateY(-2px);
          box-shadow: 0 8px 16px rgba(139, 69, 255, 0.3);
        }

        .auth-submit:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .auth-link {
          width: 100%;
          padding: 8px;
          color: var(--ga-text-subtle);
          font-size: 14px;
          transition: color 0.2s;
        }

        .auth-link:hover {
          color: var(--ga-accent);
        }

        @media (max-width: 640px) {
          .auth-container {
            align-items: flex-start;
            padding-top: 84px;
          }

          .auth-theme-toggle {
            top: 16px;
            right: 16px;
          }

          .auth-card {
            padding: 32px 24px;
          }
        }
      `}</style>
    </div>
  );
}
