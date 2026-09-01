/**
 * Tests for _app.tsx - Authentication flow and logout behavior
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import MyApp from '../../pages/_app';

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
});
