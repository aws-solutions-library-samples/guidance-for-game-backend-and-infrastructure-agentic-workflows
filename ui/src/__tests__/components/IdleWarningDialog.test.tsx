import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { IdleWarningDialog } from '@/components/IdleWarningDialog';

describe('IdleWarningDialog accessibility', () => {
  const baseProps = {
    open: true,
    remainingMs: 120_000,
    onStay: jest.fn(),
    onSignOut: jest.fn(),
  };

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('renders as a labelled modal dialog', () => {
    render(<IdleWarningDialog {...baseProps} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName();
    expect(dialog).toHaveAccessibleDescription();
  });

  it('does not render when closed', () => {
    render(<IdleWarningDialog {...baseProps} open={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('moves initial focus to the "Stay signed in" action', () => {
    render(<IdleWarningDialog {...baseProps} />);
    expect(screen.getByRole('button', { name: /stay signed in/i })).toHaveFocus();
  });

  it('restores focus to the previously focused element on close', () => {
    const trigger = document.createElement('button');
    trigger.textContent = 'open';
    document.body.appendChild(trigger);
    trigger.focus();
    expect(trigger).toHaveFocus();

    const { rerender } = render(<IdleWarningDialog {...baseProps} />);
    expect(screen.getByRole('button', { name: /stay signed in/i })).toHaveFocus();

    rerender(<IdleWarningDialog {...baseProps} open={false} />);
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it('traps Tab focus within the dialog', () => {
    render(<IdleWarningDialog {...baseProps} />);
    const stay = screen.getByRole('button', { name: /stay signed in/i });
    const signOut = screen.getByRole('button', { name: /sign out/i });

    // Forward wrap: Tab from the last control returns to the first.
    signOut.focus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' });
    expect(stay).toHaveFocus();

    // Backward wrap: Shift+Tab from the first control moves to the last.
    stay.focus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab', shiftKey: true });
    expect(signOut).toHaveFocus();
  });

  it('treats Escape as "Stay signed in" per policy (never a silent dismiss)', () => {
    const onStay = jest.fn();
    render(<IdleWarningDialog {...baseProps} onStay={onStay} />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onStay).toHaveBeenCalledTimes(1);
  });

  it('invokes the action callbacks on click', () => {
    const onStay = jest.fn();
    const onSignOut = jest.fn();
    render(<IdleWarningDialog {...baseProps} onStay={onStay} onSignOut={onSignOut} />);
    fireEvent.click(screen.getByRole('button', { name: /stay signed in/i }));
    fireEvent.click(screen.getByRole('button', { name: /sign out/i }));
    expect(onStay).toHaveBeenCalledTimes(1);
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it('shows a countdown derived from remainingMs', () => {
    render(<IdleWarningDialog {...baseProps} remainingMs={95_000} />);
    // 95s -> 1:35. The timer is hidden from assistive tech (see next test), so
    // query including hidden elements.
    const timer = screen.getByRole('timer', { hidden: true });
    expect(timer).toHaveTextContent('1:35');
  });

  it('announces politely, not assertively, and not on every second', () => {
    const { rerender } = render(<IdleWarningDialog {...baseProps} remainingMs={120_000} />);
    const status = screen.getByRole('status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    // The visible countdown timer itself must be silent to assistive tech so it
    // is not read every second.
    expect(screen.getByRole('timer', { hidden: true })).toHaveAttribute('aria-hidden', 'true');

    const initialAnnouncement = status.textContent;
    // A one-second decrement should not change the polite announcement text.
    rerender(<IdleWarningDialog {...baseProps} remainingMs={119_000} />);
    expect(status.textContent).toBe(initialAnnouncement);
  });

  it('disables actions while a refresh is in flight', () => {
    render(<IdleWarningDialog {...baseProps} busy />);
    expect(screen.getByRole('button', { name: /stay signed in/i })).toBeDisabled();
  });

  it('surfaces a refresh failure message', () => {
    render(<IdleWarningDialog {...baseProps} errorMessage="Could not extend your session. Try again." />);
    expect(screen.getByRole('alert')).toHaveTextContent(/could not extend/i);
  });
});
