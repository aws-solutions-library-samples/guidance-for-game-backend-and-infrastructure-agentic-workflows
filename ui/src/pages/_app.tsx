import '../styles/globals.css';
import '@copilotkit/react-ui/styles.css';
import type { AppProps } from 'next/app';
import Head from 'next/head';
import { useCallback, useEffect, useRef, useState } from 'react';
import CognitoAuth from '../components/CognitoAuth';
import type { CognitoUser } from 'amazon-cognito-identity-js';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import { ThemeProvider } from '../components/ThemeProvider';

const SESSION_EXPIRED_MESSAGE = 'Your session expired. Sign in again.';
const PUBLIC_API_PATHS = new Set([
  '/api/config',
  '/api/health',
  '/api/auth/login',
  '/api/auth/logout',
]);

interface Config {
  cognito: {
    region: string;
    userPoolId: string;
    clientId: string;
  };
}

function isProtectedApiRequest(input: RequestInfo | URL): boolean {
  const requestUrl = typeof Request !== 'undefined' && input instanceof Request
    ? input.url
    : input.toString();
  const url = new URL(requestUrl, window.location.origin);

  return url.origin === window.location.origin
    && url.pathname.startsWith('/api/')
    && !PUBLIC_API_PATHS.has(url.pathname);
}

function MyApp({ Component, pageProps }: AppProps) {
  const [authMode, setAuthMode] = useState<'loading' | 'skip' | 'cognito'>('loading');
  const [config, setConfig] = useState<Config | null>(null);
  const [user, setUser] = useState<CognitoUser | null>(null);
  const [authNotice, setAuthNotice] = useState('');
  const sessionExpirationInProgress = useRef(false);

  const handleSessionExpired = useCallback(() => {
    if (authMode !== 'cognito' || !user || sessionExpirationInProgress.current) {
      return;
    }

    sessionExpirationInProgress.current = true;
    try {
      user.signOut();
    } catch {
      // Continue clearing the server cookies and React state even if the Cognito
      // client cannot remove its cached session.
    }
    setUser(null);
    setAuthNotice(SESSION_EXPIRED_MESSAGE);

    void fetchWithTimeout('/api/auth/logout', { method: 'POST' })
      .catch(() => {
        // The local signed-out state is authoritative even if cookie cleanup
        // cannot reach the server. The next protected request still fails closed.
      })
      .finally(() => {
        sessionExpirationInProgress.current = false;
      });
  }, [authMode, user]);

  useEffect(() => {
    const originalFetch = window.fetch;
    const sessionAwareFetch: typeof window.fetch = async (input, init) => {
      const response = await originalFetch(input, init);
      if (response.status === 401 && isProtectedApiRequest(input)) {
        handleSessionExpired();
      }
      return response;
    };

    window.fetch = sessionAwareFetch;
    return () => {
      if (window.fetch === sessionAwareFetch) {
        window.fetch = originalFetch;
      }
    };
  }, [handleSessionExpired]);

  useEffect(() => {
    // Dev-only auth bypass is decided from build-time env (NOT from cookies):
    // the cognito_id_token cookie is HttpOnly, so it's invisible to document.cookie
    // — the old client-side cookie read was dead code. The real session check is
    // server-side (every /api route verifies the token); here we only pick which
    // top-level view to render.
    const isDev = process.env.NODE_ENV === 'development';
    const skipAuth = process.env.NEXT_PUBLIC_SKIP_AUTH === 'true';

    fetchWithTimeout('/api/config')
      .then(res => res.json())
      .then((cfg: Config) => {
        setConfig(cfg);
        setAuthMode(isDev && skipAuth ? 'skip' : 'cognito');
      })
      .catch(() => {
        // Fail CLOSED: if config can't load, require Cognito login rather than
        // silently skipping auth. Only the explicit dev bypass skips.
        setAuthMode(isDev && skipAuth ? 'skip' : 'cognito');
      });
  }, []);

  // Render the appropriate view for the current auth state.
  let content;
  if (authMode === 'loading') {
    content = (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        height: '100vh',
        background: 'var(--ga-bg-gradient)',
        fontFamily: 'Inter, system-ui'
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>🛡️</div>
          <div style={{ color: 'var(--ga-text)' }}>Loading Game Agent...</div>
        </div>
      </div>
    );
  } else if (authMode === 'skip' || user) {
    content = <Component {...pageProps} user={user} />;
  } else {
    content = (
      <CognitoAuth
        userPoolId={config?.cognito.userPoolId || ''}
        clientId={config?.cognito.clientId || ''}
        notice={authNotice}
        onAuthenticated={(cognitoUser) => {
          setAuthNotice('');
          setUser(cognitoUser);
        }}
      />
    );
  }

  // Default document title for ALL auth states (incl. the login screen, which
  // otherwise had a blank tab title). Authenticated pages may override via
  // their own <Head>.
  return (
    <ThemeProvider>
      <Head>
        <title>Game Agent - AI-Powered Game Server Management</title>
      </Head>
      {content}
    </ThemeProvider>
  );
}

export default MyApp;
