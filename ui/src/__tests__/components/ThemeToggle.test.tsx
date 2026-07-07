import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import ThemeToggle from '../../components/ThemeToggle';
import { ThemeProvider } from '../../components/ThemeProvider';

function renderThemeToggle() {
  return render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
}

describe('ThemeToggle', () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.removeAttribute('data-theme-mode');

    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: jest.fn().mockImplementation(query => ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
        addListener: jest.fn(),
        removeListener: jest.fn(),
        dispatchEvent: jest.fn(),
      })),
    });
  });

  it('defaults to system mode and resolves the current system theme', async () => {
    renderThemeToggle();

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-theme-mode', 'system');
      expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    });

    expect(screen.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('persists a selected explicit theme', async () => {
    renderThemeToggle();

    await userEvent.click(screen.getByRole('button', { name: 'Light' }));

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-theme-mode', 'light');
      expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    });

    expect(window.localStorage.getItem('game-agent-theme')).toBe('light');
    expect(screen.getByRole('button', { name: 'Light' })).toHaveAttribute('aria-pressed', 'true');
  });
});
