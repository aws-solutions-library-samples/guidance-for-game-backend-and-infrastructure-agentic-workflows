import '../styles/globals.css';
import '@copilotkit/react-ui/styles.css';
import type { AppProps } from 'next/app';
import { useEffect, useState } from 'react';
import CognitoAuth from '../components/CognitoAuth';
import type { CognitoUser } from 'amazon-cognito-identity-js';

interface Config {
  cognito: {
    region: string;
    userPoolId: string;
    clientId: string;
  };
}

function MyApp({ Component, pageProps }: AppProps) {
  const [authMode, setAuthMode] = useState<'loading' | 'skip' | 'cognito'>('loading');
  const [config, setConfig] = useState<Config | null>(null);
  const [user, setUser] = useState<CognitoUser | null>(null);

  useEffect(() => {
    fetch('/api/config')
      .then(res => res.json())
      .then((cfg: Config) => {
        setConfig(cfg);

        const isDev = process.env.NODE_ENV === 'development';
        const skipAuth = process.env.NEXT_PUBLIC_SKIP_AUTH === 'true';

        if (isDev && skipAuth) {
          setAuthMode('skip');
        } else {
          // Check if user has valid session
          const cookies = document.cookie.split(';').reduce((acc, cookie) => {
            const [key, value] = cookie.trim().split('=');
            acc[key] = value;
            return acc;
          }, {} as Record<string, string>);

          if (!cookies.cognito_id_token) {
            setUser(null);
          }
          setAuthMode('cognito');
        }
      })
      .catch(() => {
        setAuthMode('skip');
      });
  }, []);

  if (authMode === 'loading') {
    return (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        height: '100vh',
        background: 'linear-gradient(135deg, #0f0f23 0%, #1a1a2e 50%, #16213e 100%)',
        fontFamily: 'Inter, system-ui'
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>🛡️</div>
          <div style={{ color: 'white' }}>Loading Game Agent...</div>
        </div>
      </div>
    );
  }

  if (authMode === 'skip' || user) {
    return <Component {...pageProps} user={user} />;
  }

  return (
    <CognitoAuth
      userPoolId={config?.cognito.userPoolId || ''}
      clientId={config?.cognito.clientId || ''}
      onAuthenticated={(cognitoUser) => {
        setUser(cognitoUser);
      }}
    />
  );
}

export default MyApp;
