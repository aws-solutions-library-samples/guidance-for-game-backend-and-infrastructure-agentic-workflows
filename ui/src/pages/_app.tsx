import '../styles/globals.css';
import '@copilotkit/react-ui/styles.css';
import type { AppProps } from 'next/app';
import Head from 'next/head';
import { useCallback, useEffect, useRef, useState } from 'react';
import CognitoAuth from '../components/CognitoAuth';
import { IdleWarningDialog } from '../components/IdleWarningDialog';
import { useIdleSession } from '@/utils/useIdleSession';
import { createSessionCoordinator } from '@/utils/sessionCoordinator';
import type { CognitoUser } from 'amazon-cognito-identity-js';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import {
  clearSessionRefreshMarker,
  refreshSessionOnce,
  subscribeToSessionExpiration,
} from '@/utils/sessionRefresh';
import { ThemeProvider } from '../components/ThemeProvider';

const SESSION_EXPIRED_MESSAGE = 'Your session expired. Sign in again.';
const PUBLIC_API_PATHS = new Set([
  '/api/config',
  '/api/health',
  '/api/auth/login',
  '/api/auth/logout',
  '/api/auth/refresh',
]);

interface Config {
  session?: {
    absoluteLifetimeHours: number;
    idleRefreshSeconds: number;
    idleTimeoutSeconds?: number;
    idleWarningSeconds?: number;
  };
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
  const [sessionCleanupPending, setSessionCleanupPending] = useState(false);
  // Authenticated session epoch (#310, Blocker 1). Increments on each sign-in so
  // the idle controller binds to a fresh session and a prior terminal record
  // cannot immediately log the new session out.
  const [sessionEpoch, setSessionEpoch] = useState(0);
  const lastActivityAt = useRef(0);
  const userRef = useRef<CognitoUser | null>(null);
  const authModeRef = useRef(authMode);

  useEffect(() => {
    userRef.current = user;
    authModeRef.current = authMode;
  }, [user, authMode]);

  // The actual cookie-clearing + local-state logout side effect. This is the
  // LAST auth-affecting response every logout entry converges on. It is only
  // ever invoked by the coordinator (Blocker 3), so it is serialized behind any
  // in-flight refresh — a late `/api/auth/refresh` success can never write
  // cookies after this clears them.
  const performLogout = useCallback(async () => {
    if (authModeRef.current !== 'cognito' || !userRef.current) return;
    clearSessionRefreshMarker();
    setSessionCleanupPending(true);
    try {
      userRef.current.signOut();
    } catch {
      // Continue clearing the server cookies and React state even if the Cognito
      // client cannot remove its cached session.
    }
    setUser(null);
    setAuthNotice(SESSION_EXPIRED_MESSAGE);
    try {
      await fetchWithTimeout('/api/auth/logout', { method: 'POST' });
    } catch {
      // The local signed-out state is authoritative even if cookie cleanup
      // cannot reach the server. The next protected request still fails closed.
    } finally {
      setSessionCleanupPending(false);
    }
  }, []);

  // One application-level coordinator (#310, Blocker 3) shared by idle
  // expiry/sign-out, protected-request 401 expiration, refresh-failure
  // broadcasts, cross-tab logout, and the header manual sign-out. Every logout
  // waits for (or invalidates) an in-flight refresh and clears cookies last.
  //
  // Created lazily in a ref (never during render) so its closures — which read
  // identity refs — are only ever invoked from effects and event handlers.
  const coordinatorRef = useRef<ReturnType<typeof createSessionCoordinator> | null>(null);
  const getCoordinator = useCallback((): ReturnType<typeof createSessionCoordinator> => {
    if (!coordinatorRef.current) {
      coordinatorRef.current = createSessionCoordinator({
        refresh: () => refreshSessionOnce(window.fetch),
        logout: () => performLogout(),
      });
    }
    return coordinatorRef.current;
  }, [performLogout]);

  // A logout entry point usable by any caller (protected-request expiration,
  // refresh-failure broadcast, the header manual sign-out). Routes through the
  // coordinator so ordering is preserved for every path.
  const handleSessionExpired = useCallback(() => {
    void getCoordinator().logout();
  }, [getCoordinator]);

  useEffect(() => subscribeToSessionExpiration(handleSessionExpired), [handleSessionExpired]);

  // Idle-session warning (#310): a UX/local-exposure control only. The server
  // still verifies tokens on every request. "Stay signed in" runs the secure
  // #309 refresh through the shared coordinator; expiry and explicit sign-out
  // route their cookie clear through the same coordinator.
  const idle = useIdleSession({
    enabled: authMode === 'cognito' && !!user,
    config: config?.session,
    epoch: sessionEpoch,
    getCoordinator,
    refresh: useCallback(() => refreshSessionOnce(window.fetch), []),
    onLogout: handleSessionExpired,
  });

  useEffect(() => {
    if (authMode !== 'cognito' || !user) return;
    const markActivity = () => {
      lastActivityAt.current = Date.now();
    };
    window.addEventListener('pointerdown', markActivity, { passive: true });
    window.addEventListener('keydown', markActivity);
    window.addEventListener('touchstart', markActivity, { passive: true });
    return () => {
      window.removeEventListener('pointerdown', markActivity);
      window.removeEventListener('keydown', markActivity);
      window.removeEventListener('touchstart', markActivity);
    };
  }, [authMode, user]);

  useEffect(() => {
    const originalFetch = window.fetch;
    const sessionAwareFetch: typeof window.fetch = async (input, init) => {
      const retryInput = typeof Request !== 'undefined' && input instanceof Request
        ? input.clone()
        : input;
      const response = await originalFetch(input, init);
      if (response.status !== 401 || !isProtectedApiRequest(input)) return response;

      const idleRefreshMs = (config?.session?.idleRefreshSeconds ?? 900) * 1000;
      const recentlyActive = Date.now() - lastActivityAt.current <= idleRefreshMs;
      if (authMode === 'cognito' && user && recentlyActive) {
        const refreshed = await refreshSessionOnce(originalFetch);
        if (refreshed) {
          return originalFetch(retryInput, init);
        }
      }

      // Route the expiration through the coordinator so its cookie clear waits
      // for any in-flight idle refresh and lands last.
      handleSessionExpired();
      return response;
    };

    window.fetch = sessionAwareFetch;
    return () => {
      if (window.fetch === sessionAwareFetch) {
        window.fetch = originalFetch;
      }
    };
  }, [authMode, config?.session?.idleRefreshSeconds, handleSessionExpired, user]);

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
  if (authMode === 'loading' || sessionCleanupPending) {
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
          <div style={{ color: 'var(--ga-text)' }}>
            {sessionCleanupPending ? 'Ending expired session...' : 'Loading Game Agent...'}
          </div>
        </div>
      </div>
    );
  } else if (authMode === 'skip' || user) {
    content = (
      <Component
        {...pageProps}
        user={user}
        loggingOut={idle.loggingOut}
        onSignOut={handleSessionExpired}
      />
    );
  } else {
    content = (
      <CognitoAuth
        userPoolId={config?.cognito.userPoolId || ''}
        clientId={config?.cognito.clientId || ''}
        notice={authNotice}
        onAuthenticated={(cognitoUser) => {
          setAuthNotice('');
          clearSessionRefreshMarker();
          lastActivityAt.current = Date.now();
          // Begin a fresh authenticated session (#310, Blocker 1): reset the
          // coordinator's terminal state and advance the epoch so a prior
          // session's terminal idle record cannot immediately sign this one out.
          getCoordinator().beginSession();
          setSessionEpoch((epoch) => epoch + 1);
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
      <IdleWarningDialog
        open={idle.warningOpen}
        remainingMs={idle.remainingMs}
        onStay={idle.onStay}
        onSignOut={idle.onSignOut}
        busy={idle.busy}
        errorMessage={idle.errorMessage}
      />
    </ThemeProvider>
  );
}

export default MyApp;
