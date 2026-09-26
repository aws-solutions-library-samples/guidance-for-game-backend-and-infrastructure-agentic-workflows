/**
 * React binding for the idle-session controller (#310).
 *
 * Wires the pure {@link createIdleController} to React state, a polling tick
 * that re-evaluates the *absolute* deadline (so background-tab throttling
 * cannot extend the session), throttled activity listeners, and the existing
 * secure refresh (#309) and logout paths.
 *
 * This is a UX/local-exposure control only. The server continues to verify
 * tokens on every request and remains the authorization boundary.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  createIdleController,
  resolveIdleConfig,
  type IdlePhase,
  type IdleSessionConfig,
} from '@/utils/idleTimer';

const REFRESH_FAILED_MESSAGE = 'Could not extend your session. Signing you out.';

// How often the warning countdown re-renders. The controller's phase is always
// derived from the absolute deadline, so this cadence only affects display
// smoothness, never the security-relevant timing.
const TICK_INTERVAL_MS = 1_000;

export interface UseIdleSessionOptions {
  enabled: boolean;
  config: IdleSessionConfig | undefined;
  refresh: () => Promise<boolean>;
  onLogout: () => void;
}

export interface IdleSessionState {
  warningOpen: boolean;
  remainingMs: number;
  loggingOut: boolean;
  busy: boolean;
  errorMessage?: string;
  onStay: () => void;
  onSignOut: () => void;
  markActivity: () => void;
}

const ACTIVITY_EVENTS: Array<[keyof WindowEventMap, boolean]> = [
  ['pointerdown', true],
  ['keydown', false],
  ['touchstart', true],
  ['focus', false],
];

export function useIdleSession(options: UseIdleSessionOptions): IdleSessionState {
  const { enabled, config, refresh, onLogout } = options;

  const resolved = useMemo(() => resolveIdleConfig(config), [config]);
  const [phase, setPhase] = useState<IdlePhase>('active');
  const [remainingMs, setRemainingMs] = useState(resolved.idleTimeoutMs);
  const [loggingOut, setLoggingOut] = useState(false);
  const [busy, setBusy] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | undefined>(undefined);

  const controllerRef = useRef<ReturnType<typeof createIdleController> | null>(null);
  const loggingOutRef = useRef(false);
  // Keep the latest callbacks without re-subscribing the controller.
  const refreshRef = useRef(refresh);
  const onLogoutRef = useRef(onLogout);

  // Sync the latest callbacks in an effect, never during render.
  useEffect(() => {
    refreshRef.current = refresh;
    onLogoutRef.current = onLogout;
  }, [refresh, onLogout]);

  const beginLogout = useCallback(() => {
    if (loggingOutRef.current) return;
    loggingOutRef.current = true;
    setLoggingOut(true);
    setPhase('expired');
    onLogoutRef.current();
  }, []);

  useEffect(() => {
    if (!enabled) return;

    const controller = createIdleController(resolved);
    controllerRef.current = controller;
    loggingOutRef.current = false;

    controller.subscribe((next) => {
      setPhase(next);
      if (next === 'expired') beginLogout();
    });
    // Another tab signalling logout converges this tab too.
    controller.onLogout(() => beginLogout());
    controller.start();

    const markActivity = () => controller.markActivity();
    const markVisibility = () => {
      if (document.visibilityState === 'visible') controller.markActivity();
    };
    for (const [event, passive] of ACTIVITY_EVENTS) {
      window.addEventListener(event, markActivity, passive ? { passive: true } : undefined);
    }
    document.addEventListener('visibilitychange', markVisibility);

    // Drive all state updates from the async interval (never synchronously in
    // the effect body) so a re-subscribe cannot trigger a cascading render.
    // The first tick fires immediately to publish the controller's live phase,
    // remaining time, and cleared logout/error state.
    const publish = () => {
      controller.tick();
      setPhase(controller.phase());
      setRemainingMs(controller.remainingMs());
      if (!loggingOutRef.current) {
        setLoggingOut(false);
        setErrorMessage(undefined);
      }
    };
    const initialTick = window.setTimeout(publish, 0);
    const interval = window.setInterval(publish, TICK_INTERVAL_MS);

    return () => {
      window.clearTimeout(initialTick);
      window.clearInterval(interval);
      for (const [event] of ACTIVITY_EVENTS) {
        window.removeEventListener(event, markActivity);
      }
      document.removeEventListener('visibilitychange', markVisibility);
      controller.stop();
      controllerRef.current = null;
    };
  }, [enabled, resolved, beginLogout]);

  const onStay = useCallback(() => {
    const controller = controllerRef.current;
    if (!controller || busy || loggingOutRef.current) return;
    setBusy(true);
    setErrorMessage(undefined);
    void refreshRef.current()
      .then((ok) => {
        if (ok) {
          // Confirm recent activity and reset the shared deadline for all tabs.
          controller.extend();
          setPhase('active');
          setRemainingMs(controller.remainingMs());
        } else {
          setErrorMessage(REFRESH_FAILED_MESSAGE);
          beginLogout();
        }
      })
      .catch(() => {
        setErrorMessage(REFRESH_FAILED_MESSAGE);
        beginLogout();
      })
      .finally(() => setBusy(false));
  }, [busy, beginLogout]);

  const onSignOut = useCallback(() => {
    const controller = controllerRef.current;
    controller?.logout();
    beginLogout();
  }, [beginLogout]);

  const markActivity = useCallback(() => {
    controllerRef.current?.markActivity();
  }, []);

  return {
    warningOpen: enabled && phase === 'warning' && !loggingOut,
    remainingMs,
    loggingOut,
    busy,
    errorMessage,
    onStay,
    onSignOut,
    markActivity,
  };
}
