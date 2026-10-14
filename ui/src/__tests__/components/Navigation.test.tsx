/**
 * Navigation sign-out gateway (#310, Blocker 4).
 *
 * The admin Navigation header must NOT be a separate logout entry that posts
 * `/api/auth/logout` and reloads on its own — that path skips the idle terminal
 * broadcast, the chat UI lock, and the coordinator's cookie-ordering queue. When
 * the app supplies an `onSignOut` gateway, Navigation must route through it (the
 * one coordinator-backed logout that clears cookies last) and must not issue a
 * direct logout POST or a full-page reload.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import Navigation from '../../components/Navigation';
import { ThemeProvider } from '../../components/ThemeProvider';

jest.mock('next/router', () => ({
  useRouter: () => ({
    route: '/admin/users',
    pathname: '/admin/users',
    query: {},
    asPath: '/admin/users',
    push: jest.fn(),
  }),
}));

describe('Navigation sign-out gateway (#310, Blocker 4)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (global.fetch as unknown) = jest.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url === '/api/auth/user') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ username: 'Admin', email: 'admin@example.com', isAdmin: true }),
        } as Response;
      }
      if (url === '/api/auth/logout') {
        return { ok: true, status: 200, json: async () => ({ success: true }) } as Response;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
  });

  async function openMenuAndSignOut() {
    await waitFor(() => expect(screen.getByText('Admin')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Admin/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Sign Out' }));
  }

  it('routes sign-out through the provided coordinator gateway and does NOT post logout or reload', async () => {
    const onSignOut = jest.fn();
    render(
      <ThemeProvider>
        <Navigation onSignOut={onSignOut} />
      </ThemeProvider>,
    );

    await openMenuAndSignOut();

    // The gateway owns the terminal logout: it latches idle state, broadcasts,
    // and clears cookies last via the coordinator.
    expect(onSignOut).toHaveBeenCalledTimes(1);
    // No direct logout POST from the admin header.
    const fetchMock = global.fetch as jest.Mock;
    expect(
      fetchMock.mock.calls.filter(([input]) => input.toString() === '/api/auth/logout'),
    ).toHaveLength(0);
  });

  it('falls back to a direct logout only when NO gateway is supplied (dev/no-coordinator)', async () => {
    render(
      <ThemeProvider>
        <Navigation />
      </ThemeProvider>,
    );

    await openMenuAndSignOut();

    const fetchMock = global.fetch as jest.Mock;
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([input]) => input.toString() === '/api/auth/logout'),
      ).toHaveLength(1),
    );
  });
});
