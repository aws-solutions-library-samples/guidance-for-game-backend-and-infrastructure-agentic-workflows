/**
 * Accessible idle-session warning dialog (#310).
 *
 * A modal countdown that appears shortly before automatic sign-out. It is a
 * user-experience control only; the server still enforces token and
 * absolute-lifetime policy on every request.
 *
 * Accessibility contract:
 * - role="dialog" + aria-modal, labelled by its heading and described by its body.
 * - Focus moves to "Stay signed in" on open and is restored to the previously
 *   focused element on close.
 * - Tab/Shift+Tab are trapped and wrap within the dialog.
 * - Escape is defined by policy as "Stay signed in" (an intentional keep-alive),
 *   never a silent dismiss that would leave an unattended screen authenticated.
 * - The per-second countdown is aria-hidden so assistive tech is not interrupted
 *   every second; a separate polite status region carries a coarse announcement.
 */

'use client';

import React, { useEffect, useMemo, useRef } from 'react';

interface IdleWarningDialogProps {
  open: boolean;
  remainingMs: number;
  onStay: () => void;
  onSignOut: () => void;
  busy?: boolean;
  errorMessage?: string;
}

function formatCountdown(remainingMs: number): string {
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

// Coarse, low-frequency announcement so the polite live region changes at most
// a couple of times rather than every second.
function coarseAnnouncement(remainingMs: number): string {
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  if (totalSeconds <= 0) return 'Signing you out now.';
  if (totalSeconds <= 30) return 'Signing out in less than 30 seconds unless you stay signed in.';
  const minutes = Math.max(1, Math.round(totalSeconds / 60));
  return `Your session will end in about ${minutes} minute${minutes === 1 ? '' : 's'} unless you stay signed in.`;
}

const FOCUSABLE = 'button:not([disabled])';

export function IdleWarningDialog({
  open,
  remainingMs,
  onStay,
  onSignOut,
  busy = false,
  errorMessage,
}: IdleWarningDialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const stayRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocused = useRef<Element | null>(null);

  const announcement = useMemo(() => coarseAnnouncement(remainingMs), [remainingMs]);

  useEffect(() => {
    if (!open) return;
    // Remember what had focus so we can restore it when the dialog closes.
    previouslyFocused.current = document.activeElement;
    // Initial focus target: the primary, least-destructive action.
    stayRef.current?.focus();
    return () => {
      const toRestore = previouslyFocused.current;
      if (toRestore instanceof HTMLElement && document.contains(toRestore)) {
        toRestore.focus();
      }
    };
  }, [open]);

  if (!open) return null;

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      // Policy: Escape keeps the session (explicit keep-alive), never dismiss.
      event.preventDefault();
      onStay();
      return;
    }
    if (event.key !== 'Tab') return;
    const container = dialogRef.current;
    if (!container) return;
    const focusable = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="ga-idle-overlay">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="ga-idle-title"
        aria-describedby="ga-idle-desc"
        className="ga-idle-dialog"
        onKeyDown={handleKeyDown}
      >
        <h2 id="ga-idle-title" className="ga-idle-title">Still there?</h2>
        <p id="ga-idle-desc" className="ga-idle-desc">
          You have been inactive. To protect your account, you will be signed out
          automatically. Choose “Stay signed in” to continue your session.
        </p>

        <div className="ga-idle-countdown">
          <span
            role="timer"
            aria-hidden="true"
            className="ga-idle-timer"
          >
            {formatCountdown(remainingMs)}
          </span>
        </div>

        {/* Coarse, polite announcement — changes rarely, not every second. */}
        <div role="status" aria-live="polite" className="ga-visually-hidden">
          {announcement}
        </div>

        {errorMessage ? (
          <p role="alert" className="ga-idle-error">{errorMessage}</p>
        ) : null}

        <div className="ga-idle-actions">
          <button
            ref={stayRef}
            type="button"
            className="ga-idle-primary"
            aria-label="Stay signed in"
            onClick={onStay}
            disabled={busy}
          >
            {busy ? 'Staying signed in…' : 'Stay signed in'}
          </button>
          <button
            type="button"
            className="ga-idle-secondary"
            onClick={onSignOut}
            disabled={busy}
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
