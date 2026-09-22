import { useEffect, useRef } from 'react';

interface ConfirmDialogProps {
  title: string;
  children: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * A small accessible modal confirmation dialog:
 *  - role="dialog" + aria-modal, labelled by its title;
 *  - moves focus to the confirm button on open and restores focus on close;
 *  - traps Tab within the dialog and closes on Escape.
 */
export default function ConfirmDialog({
  title,
  children,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    confirmRef.current?.focus();
    return () => {
      previouslyFocused.current?.focus?.();
    };
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape' && !busy) {
      e.stopPropagation();
      onCancel();
      return;
    }
    if (e.key !== 'Tab') return;
    const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled])',
    );
    if (!focusables || focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="ga-cd-overlay" onKeyDown={handleKeyDown}>
      <div
        ref={dialogRef}
        className="ga-cd-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ga-cd-title"
      >
        <h2 id="ga-cd-title" className="ga-cd-title">
          {title}
        </h2>
        <div className="ga-cd-body">{children}</div>
        <div className="ga-cd-actions">
          <button type="button" className="ga-cd-cancel" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={danger ? 'ga-cd-confirm ga-cd-danger' : 'ga-cd-confirm'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
      <style jsx>{`
        .ga-cd-overlay {
          position: fixed; inset: 0; background: var(--ga-overlay-bg);
          display: flex; align-items: center; justify-content: center; z-index: 2000;
        }
        .ga-cd-dialog {
          background: var(--ga-surface-elevated); border: 1px solid var(--ga-accent-border);
          border-radius: 12px; padding: 24px; max-width: 460px; width: calc(100% - 32px);
          box-shadow: var(--ga-elevated-shadow); color: var(--ga-text);
        }
        .ga-cd-title { margin: 0 0 12px; font-size: 18px; }
        .ga-cd-body { color: var(--ga-text-muted); font-size: 14px; margin-bottom: 20px; }
        .ga-cd-actions { display: flex; justify-content: flex-end; gap: 12px; }
        .ga-cd-cancel, .ga-cd-confirm {
          border-radius: 8px; padding: 8px 18px; font-weight: 600; cursor: pointer; font-size: 14px;
        }
        .ga-cd-cancel { background: var(--ga-control-hover-bg); border: 1px solid var(--ga-border); color: var(--ga-text); }
        .ga-cd-confirm { background: var(--ga-accent); border: 1px solid var(--ga-accent-border-strong); color: #fff; }
        .ga-cd-confirm.ga-cd-danger { background: var(--ga-danger); border-color: var(--ga-danger-border); color: #fff; }
        .ga-cd-cancel:disabled, .ga-cd-confirm:disabled { opacity: 0.6; cursor: not-allowed; }
        .ga-cd-cancel:focus-visible, .ga-cd-confirm:focus-visible { outline: 2px solid var(--ga-accent); outline-offset: 2px; }
      `}</style>
    </div>
  );
}
