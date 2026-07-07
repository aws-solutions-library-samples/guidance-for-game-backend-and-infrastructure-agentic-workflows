/**
 * Tests for CognitoAuth component - Error handling for fetch and JSON.stringify
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import CognitoAuth from '../../components/CognitoAuth';
import { ThemeProvider } from '../../components/ThemeProvider';

// Mock amazon-cognito-identity-js
const mockAuthenticateUser = jest.fn();
const mockGetAccessToken = jest.fn();
const mockGetIdToken = jest.fn();
const mockGetRefreshToken = jest.fn();

jest.mock('amazon-cognito-identity-js', () => ({
  CognitoUserPool: jest.fn().mockImplementation(() => ({})),
  CognitoUser: jest.fn().mockImplementation(() => ({
    authenticateUser: mockAuthenticateUser,
  })),
  AuthenticationDetails: jest.fn(),
  CognitoUserAttribute: jest.fn(),
}));

// Mock fetch
global.fetch = jest.fn();

describe('CognitoAuth - handleSignIn error handling', () => {
  const mockOnAuthenticated = jest.fn();
  const mockSession = {
    getAccessToken: mockGetAccessToken,
    getIdToken: mockGetIdToken,
    getRefreshToken: mockGetRefreshToken,
  };

  const renderAuth = () => render(
    <ThemeProvider>
      <CognitoAuth
        userPoolId="test-pool"
        clientId="test-client"
        onAuthenticated={mockOnAuthenticated}
      />
    </ThemeProvider>,
  );

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetAccessToken.mockReturnValue({ getJwtToken: () => 'access-token' });
    mockGetIdToken.mockReturnValue({ getJwtToken: () => 'id-token' });
    mockGetRefreshToken.mockReturnValue({ getToken: () => 'refresh-token' });
  });

  it('handles network error when storing tokens', async () => {
    (global.fetch as jest.Mock).mockRejectedValue(new Error('Network error'));

    mockAuthenticateUser.mockImplementation((authDetails, callbacks) => {
      callbacks.onSuccess(mockSession);
    });

    renderAuth();

    const emailInput = screen.getByPlaceholderText('your@email.com');
    const passwordInput = screen.getByPlaceholderText('••••••••');

    fireEvent.change(emailInput, { target: { value: 'test@example.com' } });
    fireEvent.change(passwordInput, { target: { value: 'password123' } });
    fireEvent.submit(emailInput.closest('form')!);

    await waitFor(() => {
      expect(screen.getByText(/failed to store session/i)).toBeInTheDocument();
    }, { timeout: 3000 });

    expect(mockOnAuthenticated).not.toHaveBeenCalled();
  });

  it('handles API error response when storing tokens', async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: false,
      status: 500,
    });

    mockAuthenticateUser.mockImplementation((authDetails, callbacks) => {
      callbacks.onSuccess(mockSession);
    });

    renderAuth();

    const emailInput = screen.getByPlaceholderText('your@email.com');
    const passwordInput = screen.getByPlaceholderText('••••••••');

    fireEvent.change(emailInput, { target: { value: 'test@example.com' } });
    fireEvent.change(passwordInput, { target: { value: 'password123' } });
    fireEvent.submit(emailInput.closest('form')!);

    await waitFor(() => {
      expect(screen.getByText(/failed to store session/i)).toBeInTheDocument();
    }, { timeout: 3000 });

    expect(mockOnAuthenticated).not.toHaveBeenCalled();
  });

  it('handles JSON.stringify error with circular reference', async () => {
    const circularSession = {
      getAccessToken: () => ({ getJwtToken: () => 'token' }),
      getIdToken: () => ({ getJwtToken: () => 'token' }),
      getRefreshToken: () => {
        const obj: { token?: string; self?: unknown } = { token: 'token' };
        obj.self = obj;
        return obj;
      },
    };

    mockAuthenticateUser.mockImplementation((authDetails, callbacks) => {
      callbacks.onSuccess(circularSession);
    });

    renderAuth();

    const emailInput = screen.getByPlaceholderText('your@email.com');
    const passwordInput = screen.getByPlaceholderText('••••••••');

    fireEvent.change(emailInput, { target: { value: 'test@example.com' } });
    fireEvent.change(passwordInput, { target: { value: 'password123' } });
    fireEvent.submit(emailInput.closest('form')!);

    await waitFor(() => {
      expect(screen.getByText(/failed to store session/i)).toBeInTheDocument();
    }, { timeout: 3000 });

    expect(mockOnAuthenticated).not.toHaveBeenCalled();
  });

  it('successfully stores tokens and calls onAuthenticated', async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
    });

    mockAuthenticateUser.mockImplementation((authDetails, callbacks) => {
      callbacks.onSuccess(mockSession);
    });

    renderAuth();

    const emailInput = screen.getByPlaceholderText('your@email.com');
    const passwordInput = screen.getByPlaceholderText('••••••••');

    fireEvent.change(emailInput, { target: { value: 'test@example.com' } });
    fireEvent.change(passwordInput, { target: { value: 'password123' } });
    fireEvent.submit(emailInput.closest('form')!);

    await waitFor(() => {
      expect(mockOnAuthenticated).toHaveBeenCalled();
    }, { timeout: 3000 });

    // fetchWithTimeout adds an AbortController signal to the options, so match
    // the meaningful fields with objectContaining rather than the exact object.
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/auth/login',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          accessToken: 'access-token',
          idToken: 'id-token',
          refreshToken: 'refresh-token',
        }),
        signal: expect.anything(),
      })
    );
  });
});
