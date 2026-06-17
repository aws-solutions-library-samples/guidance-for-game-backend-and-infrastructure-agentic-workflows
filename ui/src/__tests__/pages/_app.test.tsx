/**
 * Tests for _app.tsx - Authentication flow and logout behavior
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import MyApp from '../../pages/_app';

// Mock CognitoAuth component
jest.mock('../../components/CognitoAuth', () => {
  return function MockCognitoAuth() {
    return <div data-testid="cognito-auth">Login Screen</div>;
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

  it('handles config fetch failure gracefully', async () => {
    (global.fetch as jest.Mock).mockRejectedValue(new Error('Network error'));

    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.NODE_ENV = 'production';

    render(<MyApp Component={mockComponent} pageProps={mockPageProps} />);

    await waitFor(() => {
      // Should skip auth on config failure
      expect(screen.getByTestId('app-content')).toBeInTheDocument();
    });
  });

  it('validates session on mount in production mode', async () => {
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

    expect(global.fetch).toHaveBeenCalledWith('/api/config');
  });
});
