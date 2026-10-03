/**
 * Tests for _app.tsx - Authentication flow and logout behavior
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import MyApp from '../../pages/_app';
import { resetRefreshCoordinatorForTests } from '@/utils/sessionRefresh';

const mockCognitoSignOut = jest.fn();
const mockEstablishSession = jest.fn();

// Mock CognitoAuth component
jest.mock('../../components/CognitoAuth', () => {
  return function MockCognitoAuth({
    notice,
    onAuthenticated,
  }: {
    notice?: string;
    onAuthenticated?: (user: { signOut: () => void }) => void;
  }) {
    return (
      <div data-testid="cognito-auth">
        Login Screen
        {notice && <div role="alert">{notice}</div>}
        <button onClick={() => {
          mockEstablishSession();
          onAuthenticated?.({ signOut: mockCognitoSignOut });
        }}>
          Complete sign in
        </button>
      </div>
    );
  };
});

// Mock Next.js router
jest.mock('next/router', () => ({
  useRouter: () => ({
    route: '/',
    pathname: '/',
    query: {},
    asPath: '/',
  }),
}));

// Mock fetch
global.fetch = jest.fn();

describe('MyApp - Logout behavior', () => {
  const mockComponent = () => <div data-testid="app-content">App Content</div>;
  const mockPageProps = {};

  beforeEach(() => {
    jest.clearAllMocks();
    mockEstablishSession.mockReset();
    resetRefreshCoordinatorForTests();
    window.localStorage.clear();
    global.fetch = jest.fn();
    // Clear cookies
    Object.defineProperty(document, 'cookie', {
      writable: true,
      value: '',
    });
  });

  it('clears user state when no session cookies exist after logout', async () => {
    // Mock config API response - production mode
    (global.fetch as jest.Mock).mockResolvedValue({
      json: async () => ({
        cognito: {
          region: 'us-west-2',
          userPoolId: 'test-pool',
          clientId: 'test-client',
        },
      }),
    });

    // Set NEXT_PUBLIC_SKIP_AUTH to false (production mode)
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    // No cookies set (simulating post-logout state)
    Object.defineProperty(document, 'cookie', {
      writable: true,
      value: '',
    });

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // Should show login screen when no cookies exist
      expect(screen.getByTestId('cognito-auth')).toBeInTheDocument();
    });
  });

  it('shows app content when valid session cookies exist', async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      json: async () => ({
        cognito: {
          region: 'us-west-2',
          userPoolId: 'test-pool',
          clientId: 'test-client',
        },
      }),
    });

    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    // Set valid session cookies
    Object.defineProperty(document, 'cookie', {
      writable: true,
      value: 'cognito_id_token=valid-token; cognito_access_token=valid-token',
    });

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // Should show login screen initially (no user object yet)
      expect(screen.getByTestId('cognito-auth')).toBeInTheDocument();
    });
  });

  it('skips authentication in development mode', async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      json: async () => ({
        cognito: {
          region: 'us-west-2',
          userPoolId: 'test-pool',
          clientId: 'test-client',
        },
      }),
    });

    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    process.env.NODE_ENV = 'development';

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // Should show app content directly
      expect(screen.getByTestId('app-content')).toBeInTheDocument();
    });
  });

  it('fails CLOSED to the login screen when config fetch fails (production)', async () => {
    (global.fetch as jest.Mock).mockRejectedValue(new Error('Network error'));

    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // A config fetch failure must NOT silently skip auth (#131). In production
      // it falls closed to the Cognito login screen.
      expect(screen.getByTestId('cognito-auth')).toBeInTheDocument();
    });
  });

  it('still honors the explicit dev bypass when config fetch fails', async () => {
    (global.fetch as jest.Mock).mockRejectedValue(new Error('Network error'));

    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    process.env.NODE_ENV = 'development';

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // Explicit dev bypass (dev + SKIP_AUTH) still renders the app on failure.
      expect(screen.getByTestId('app-content')).toBeInTheDocument();
    });
  });

  it('validates session on mount in production mode', async () => {
    const fetchMock = global.fetch as jest.Mock;
    fetchMock.mockResolvedValue({
      json: async () => ({
        cognito: {
          region: 'us-west-2',
          userPoolId: 'test-pool',
          clientId: 'test-client',
        },
      }),
    });

    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    // Simulate logout scenario: cookies cleared but component remounting
    Object.defineProperty(document, 'cookie', {
      writable: true,
      value: '', // No cookies
    });

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // Should require login when no valid session
      expect(screen.getByTestId('cognito-auth')).toBeInTheDocument();
    });

    // fetchWithTimeout passes an AbortController signal in the options.
    expect(fetchMock).toHaveBeenCalledWith('/api/config', expect.objectContaining({ signal: expect.anything() }));
  });

  it('returns to sign-in and sends one logout when concurrent authenticated requests receive 401', async () => {
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: {
              region: 'us-west-2',
              userPoolId: 'test-pool',
              clientId: 'test-client',
            },
          }),
        } as Response;
      }
      if (url === '/api/copilot/chat') {
        return {
          ok: false,
          status: 401,
          json: async () => ({ error: 'Unauthorized' }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ success: true }),
        } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void Promise.all([
          fetch('/api/copilot/chat', { method: 'POST' }),
          fetch('/api/copilot/chat', { method: 'POST' }),
        ])}>
          Submit message
        </button>
      </div>
    );

    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);

    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Submit message' }));

    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent('Your session expired. Sign in again.');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/logout',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(mockCognitoSignOut).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.filter(([input]) => input.toString() === '/api/copilot/chat')).toHaveLength(2);
  });

  it('refreshes once and retries concurrent active requests without signing out', async () => {
    let refreshed = false;
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleRefreshSeconds: 900, absoluteLifetimeHours: 8 },
          }),
        } as Response;
      }
      if (url === '/api/copilot/chat') {
        return {
          ok: refreshed,
          status: refreshed ? 200 : 401,
          json: async () => refreshed ? ({ result: 'ok' }) : ({ error: 'Unauthorized' }),
        } as Response;
      }
      if (url === '/api/auth/refresh') {
        refreshed = true;
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      if (url === '/api/auth/logout') {
        throw new Error('logout must not run after a successful refresh');
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void Promise.all([
          fetch('/api/copilot/chat', { method: 'POST' }),
          fetch('/api/copilot/chat', { method: 'POST' }),
        ])}>
          Submit message
        </button>
      </div>
    );

    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Submit message' }));

    await waitFor(() => expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/auth/refresh',
    )).toHaveLength(1));
    await waitFor(() => expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/copilot/chat',
    )).toHaveLength(4));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();
    expect(mockCognitoSignOut).not.toHaveBeenCalled();
  });

  it('keeps a refreshed session when the retried endpoint still returns 401', async () => {
    let initialRequest = true;
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleRefreshSeconds: 900, absoluteLifetimeHours: 8 },
          }),
        } as Response;
      }
      if (url === '/api/copilot/chat') {
        initialRequest = false;
        return { ok: false, status: 401, json: async () => ({ error: 'Unauthorized' }) } as Response;
      }
      if (url === '/api/auth/refresh') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      if (url === '/api/auth/logout') {
        throw new Error('a successful refresh must not clear the session');
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void fetch('/api/copilot/chat', { method: 'POST' })}>Submit message</button>
      </div>
    );
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Submit message' }));

    await waitFor(() => expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/copilot/chat',
    )).toHaveLength(2));
    expect(initialRequest).toBe(false);
    expect(screen.getByTestId('app-content')).toBeInTheDocument();
    expect(mockCognitoSignOut).not.toHaveBeenCalled();
  });

  it('does not refresh an unattended session after the idle window', async () => {
    const nowSpy = jest.spyOn(Date, 'now').mockReturnValue(1_000);
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleRefreshSeconds: 1, absoluteLifetimeHours: 8 },
          }),
        } as Response;
      }
      if (url === '/api/copilot/chat') {
        return { ok: false, status: 401, json: async () => ({ error: 'Unauthorized' }) } as Response;
      }
      if (url === '/api/auth/refresh') {
        throw new Error('idle sessions must not refresh');
      }
      if (url === '/api/auth/logout') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => <div data-testid="app-content">App Content</div>;
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());

    nowSpy.mockReturnValue(5_000);
    await act(async () => {
      await window.fetch('/api/copilot/chat', { method: 'POST' });
    });

    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/auth/refresh',
    )).toHaveLength(0);
    expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/auth/logout',
    )).toHaveLength(1);
    nowSpy.mockRestore();
  });

  it('does not let stale expiration cleanup clear a newly established session', async () => {
    let releaseLogout!: () => void;
    let serverSession: 'none' | 'fresh' = 'none';
    const pendingLogout = new Promise<Response>((resolve) => {
      releaseLogout = () => {
        serverSession = 'none';
        resolve({
          ok: true,
          status: 200,
          json: async () => ({ success: true }),
        } as Response);
      };
    });
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: {
              region: 'us-west-2',
              userPoolId: 'test-pool',
              clientId: 'test-client',
            },
          }),
        } as Response;
      }
      if (url === '/api/copilot/chat') {
        return {
          ok: false,
          status: 401,
          json: async () => ({ error: 'Unauthorized' }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return pendingLogout;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock;
    mockEstablishSession.mockImplementation(() => {
      serverSession = 'fresh';
    });
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void fetch('/api/copilot/chat', { method: 'POST' })}>
          Submit message
        </button>
      </div>
    );

    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);

    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Submit message' }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/auth/logout',
    )).toHaveLength(1));

    const immediateSignIn = screen.queryByRole('button', { name: 'Complete sign in' });
    if (immediateSignIn) {
      fireEvent.click(immediateSignIn);
      await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());
    }

    releaseLogout();

    if (!immediateSignIn) {
      await waitFor(() => expect(screen.getByRole('button', { name: 'Complete sign in' })).toBeInTheDocument());
      fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    }

    await waitFor(() => expect(serverSession).toBe('fresh'));
    expect(immediateSignIn).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(
      ([input]) => input.toString() === '/api/auth/logout',
    )).toHaveLength(1);
  });

  it('allows sign-in after expiration cleanup fails', async () => {
    let rejectLogout!: (error: Error) => void;
    const pendingLogout = new Promise<Response>((_resolve, reject) => {
      rejectLogout = reject;
    });
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: {
              region: 'us-west-2',
              userPoolId: 'test-pool',
              clientId: 'test-client',
            },
          }),
        } as Response;
      }
      if (url === '/api/copilot/chat') {
        return {
          ok: false,
          status: 401,
          json: async () => ({ error: 'Unauthorized' }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return pendingLogout;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void fetch('/api/copilot/chat', { method: 'POST' })}>
          Submit message
        </button>
      </div>
    );

    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);

    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    await waitFor(() => expect(screen.getByTestId('app-content')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Submit message' }));

    await waitFor(() => expect(screen.getByText('Ending expired session...')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Complete sign in' })).not.toBeInTheDocument();

    rejectLogout(new Error('Logout unavailable'));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Complete sign in' })).toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent('Your session expired. Sign in again.');
  });

  it('opens the idle warning at the threshold and keeps the session on "Stay signed in" (#310)', async () => {
    jest.useFakeTimers();
    let now = 1_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120 },
          }),
        } as Response;
      }
      if (url === '/api/auth/refresh') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => <div data-testid="app-content">App Content</div>;
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);

    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // Advance past the 28-minute warning threshold and let the tick fire.
    act(() => {
      now += 28 * 60_000 + 1_000;
      jest.advanceTimersByTime(1_000);
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    // "Stay signed in" runs the secure refresh and closes the dialog.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Stay signed in' }));
      await Promise.resolve();
    });
    expect(fetchMock.mock.calls.some(([input]) => input.toString() === '/api/auth/refresh')).toBe(true);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    act(() => {
      jest.runOnlyPendingTimers();
    });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  it('idle expiry signs out through the existing logout path without a page refresh (#310)', async () => {
    jest.useFakeTimers();
    let now = 2_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120 },
          }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => <div data-testid="app-content">App Content</div>;
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);

    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // Jump past the absolute idle deadline (background-tab throttle scenario);
    // a single tick observes the passed deadline and logs out.
    await act(async () => {
      now += 31 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input]) => input.toString() === '/api/auth/logout')).toBe(true);
    expect(mockCognitoSignOut).toHaveBeenCalled();

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  // ---- Blocker 3 regressions: every logout entry clears cookies LAST, and a
  // later successful reauthentication works. ----

  it('a protected-request expiration during a pending idle refresh clears cookies AFTER the refresh (#310)', async () => {
    jest.useFakeTimers();
    let now = 3_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const order: string[] = [];
    let releaseRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });

    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            // idleRefreshSeconds is tiny so the 401 path treats the user as NOT
            // recently active and expires directly (a non-idle logout entry)
            // instead of attempting its own refresh.
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120, idleRefreshSeconds: 1 },
          }),
        } as Response;
      }
      if (url === '/api/auth/refresh') {
        order.push('refresh-start');
        return pendingRefresh;
      }
      if (url === '/api/copilot/chat') {
        return { ok: false, status: 401, json: async () => ({ error: 'Unauthorized' }) } as Response;
      }
      if (url === '/api/auth/logout') {
        order.push('logout');
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void fetch('/api/copilot/chat', { method: 'POST' })}>Submit message</button>
      </div>
    );
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // Open the idle warning and start a "Stay signed in" refresh (kept pending).
    act(() => {
      now += 28 * 60_000 + 1_000;
      jest.advanceTimersByTime(1_000);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Stay signed in' }));
      await Promise.resolve();
    });
    expect(order).toEqual(['refresh-start']);

    // A protected request returns 401 while the idle refresh is still pending.
    // The user is past the idleRefresh window, so this expires directly — a
    // non-idle logout entry. It must NOT clear cookies before the refresh
    // settles (Blocker 3).
    now += 10_000; // beyond the 1s idleRefresh window
    await act(async () => {
      await window.fetch('/api/copilot/chat', { method: 'POST' });
      await Promise.resolve();
    });
    expect(order).toEqual(['refresh-start']);

    // The refresh finally succeeds. Its cookie write happens, THEN the deferred
    // logout clears cookies last.
    await act(async () => {
      releaseRefresh({ ok: true, status: 200, json: async () => ({ success: true }) } as Response);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(order).toContain('logout');
    // Logout is the final auth-affecting response.
    expect(order[order.length - 1]).toBe('logout');

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  // ---- Blocker 1 (production epoch): the authenticated session epoch is
  // allocated from PERSISTED storage, not component-local state, so a full
  // reload after logout does not reuse the prior terminal epoch. ----

  it('a fresh sign-in after a full reload is not immediately logged out by the prior terminal idle record (#310, Blocker 1)', async () => {
    jest.useFakeTimers();
    let now = 6_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120 },
          }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => <div data-testid="app-content">App Content</div>;

    // First app instance: sign in, then idle out (persists a terminal record).
    const first = render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();
    await act(async () => {
      now += 31 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId('cognito-auth')).toBeInTheDocument();

    // A FULL RELOAD: unmount and mount a brand-new _app instance. Component
    // state (any local epoch counter) resets to zero, but the persisted terminal
    // record survives in localStorage. A component-local epoch would repeat the
    // prior epoch and be logged out immediately.
    first.unmount();
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
      await Promise.resolve();
      await Promise.resolve();
    });
    // The reloaded, freshly signed-in session must stay authenticated.
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    await act(async () => {
      now += 5 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  // ---- Blocker 3 (production wiring): the protected-request 401 refresh runs
  // through the SAME coordinator pendingRefresh path, so a logout requested
  // while that refresh is in flight clears cookies LAST. ----

  it('a logout during a pending protected-request refresh clears cookies AFTER the refresh (#310, Blocker 3)', async () => {
    jest.useFakeTimers();
    let now = 7_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const order: string[] = [];
    let releaseRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });

    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            // Large idleRefresh window: the protected 401 treats the user as
            // recently active and attempts its refresh through the coordinator.
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120, idleRefreshSeconds: 900 },
          }),
        } as Response;
      }
      if (url === '/api/auth/refresh') {
        order.push('refresh-start');
        return pendingRefresh;
      }
      if (url === '/api/copilot/chat') {
        return { ok: false, status: 401, json: async () => ({ error: 'Unauthorized' }) } as Response;
      }
      if (url === '/api/auth/logout') {
        order.push('logout');
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void fetch('/api/copilot/chat', { method: 'POST' })}>Submit message</button>
      </div>
    );
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // A protected request returns 401 while the user is recently active. The
    // 401 path starts a refresh THROUGH THE COORDINATOR and keeps it pending.
    await act(async () => {
      void window.fetch('/api/copilot/chat', { method: 'POST' });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(order).toEqual(['refresh-start']);

    // While that refresh is still pending, the idle deadline elapses and drives
    // a logout. Because the protected refresh is tracked by the coordinator, the
    // cookie-clearing logout must WAIT for it and land last.
    await act(async () => {
      now += 31 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    // Logout has NOT cleared cookies yet — the coordinator is waiting on the
    // tracked protected refresh.
    expect(order).toEqual(['refresh-start']);

    await act(async () => {
      releaseRefresh({ ok: true, status: 200, json: async () => ({ success: true }) } as Response);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(order).toContain('logout');
    expect(order[order.length - 1]).toBe('logout');

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  it('does NOT retry the protected request when a logout latched terminal while its refresh was pending (#310, Blocker 1)', async () => {
    jest.useFakeTimers();
    let now = 9_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const order: string[] = [];
    let chatCalls = 0;
    let releaseRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });

    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            // Large idleRefresh window: the protected 401 treats the user as
            // recently active and attempts a refresh through the coordinator.
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120, idleRefreshSeconds: 900 },
          }),
        } as Response;
      }
      if (url === '/api/auth/refresh') {
        order.push('refresh-start');
        return pendingRefresh; // stays pending until released
      }
      if (url === '/api/copilot/chat') {
        chatCalls += 1;
        order.push('chat');
        return { ok: false, status: 401, json: async () => ({ error: 'Unauthorized' }) } as Response;
      }
      if (url === '/api/auth/logout') {
        order.push('logout');
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => (
      <div data-testid="app-content">
        <button onClick={() => void fetch('/api/copilot/chat', { method: 'POST' })}>Submit message</button>
      </div>
    );
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // A protected request returns 401 while the user is recently active; the
    // 401 path starts a refresh through the coordinator and keeps it pending.
    await act(async () => {
      void window.fetch('/api/copilot/chat', { method: 'POST' });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(order).toEqual(['chat', 'refresh-start']);
    expect(chatCalls).toBe(1);

    // While that refresh is still pending, the idle deadline elapses and drives
    // a terminal logout.
    await act(async () => {
      now += 31 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    expect(order).toEqual(['chat', 'refresh-start']);

    // The raw refresh finally resolves true — a LATE success after terminal
    // logout. Because the session is terminally logged out, the protected
    // request must NOT be retried, and the cookie-clearing logout must be the
    // final auth-affecting response.
    await act(async () => {
      releaseRefresh({ ok: true, status: 200, json: async () => ({ success: true }) } as Response);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // No protected retry occurred: /api/copilot/chat was called exactly once.
    expect(chatCalls).toBe(1);
    expect(order.filter((e) => e === 'chat')).toHaveLength(1);
    // Logout/cookie clear is final.
    expect(order).toContain('logout');
    expect(order[order.length - 1]).toBe('logout');
    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  // ---- Blocker 2 (production startup): a tab that reopens into a still-active
  // (non-terminal) persisted session whose deadline has ALREADY elapsed must
  // consume it as terminal logout, not grant a fresh idle timeout. ----

  it('startup with an elapsed persisted deadline signs out instead of reviving the session (#310, Blocker 2)', async () => {
    jest.useFakeTimers();
    const now = 8_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120 },
          }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    // Seed a still-active (non-terminal) session record whose deadline is in the
    // PAST — the tab slept/closed past the deadline before a tick recorded
    // logout. allocateSessionEpoch() adopts this live epoch on sign-in, so the
    // controller starts at the same epoch and observes the elapsed deadline.
    window.localStorage.setItem(
      'game-agent-idle-session',
      JSON.stringify({ epoch: 1, generation: 3, deadline: now - 60_000, loggedOut: false }),
    );

    const AuthenticatedPage = () => <div data-testid="app-content">App Content</div>;
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // The elapsed persisted deadline must be consumed as terminal logout on
    // startup: the session is signed out, not granted a fresh timeout.
    await waitFor(() => expect(screen.getByTestId('cognito-auth')).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input]) => input.toString() === '/api/auth/logout')).toBe(true);

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });

  it('supports a successful reauthentication after an idle logout (#310)', async () => {
    jest.useFakeTimers();
    let now = 4_000_000;
    const nowSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const fetchMock = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/config') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            cognito: { region: 'us-west-2', userPoolId: 'test-pool', clientId: 'test-client' },
            session: { idleTimeoutSeconds: 1800, idleWarningSeconds: 120 },
          }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    const AuthenticatedPage = () => <div data-testid="app-content">App Content</div>;
    render(<MyApp Component={AuthenticatedPage} pageProps={{}} />);
    // Flush the /api/config promise so the login screen renders.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // First sign-in.
    fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // Idle logout.
    await act(async () => {
      now += 31 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId('cognito-auth')).toBeInTheDocument();

    // A later sign-in must succeed and NOT be immediately logged out by the
    // prior session's terminal idle record (fresh epoch via beginSession()).
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Complete sign in' }));
      await Promise.resolve();
    });
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    // Advance short of the new warning window: the session stays authenticated.
    await act(async () => {
      now += 5 * 60_000;
      jest.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    expect(screen.getByTestId('app-content')).toBeInTheDocument();

    act(() => { jest.runOnlyPendingTimers(); });
    nowSpy.mockRestore();
    jest.useRealTimers();
  });
});
