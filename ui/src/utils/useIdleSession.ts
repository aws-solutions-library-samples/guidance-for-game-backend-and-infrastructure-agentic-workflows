/**
 * React binding for the idle-session controller (#310).
 *
 * Wires the pure {@link createIdleController} to React state, a polling tick
 * that re-evaluates the *absolute* deadline (so background-tab throttling
 * cannot extend the session), throttled activity listeners, and the shared
 * application-level {@link SessionCoordinator} (Blocker 3) that serializes the
 * secure refresh (#309) against every logout entry.
 *
 * Refresh/logout ordering no longer lives in this hook's closure. It lives in
 * one coordinator shared with `_app`'s protected-request expiration,
 * refresh-failure broadcasts, cross-tab logout, and the header manual sign-out,
 * so a late refresh success can never write auth cookies after any logout.
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
import {
  createSessionCoordinator,
  type SessionCoordinator,
} from '@/utils/sessionCoordinator';

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
  /**
   * Authenticated session epoch. Increment on each successful authentication so
   * the controller binds to a fresh session and a prior terminal record cannot
   * immediately log the new session out.
   */
  epoch?: number;
  /**
   * Shared application-level coordinator accessor (Blocker 3). When omitted the
   * hook builds its own from `refresh`/`onLogout` (used by the focused hook
   * tests). In production `_app` supplies the shared coordinator so every logout
   * entry — not just idle expiry/sign-out — serializes against the same refresh.
   * Passed as a getter so the instance is resolved inside the hook's effect,
   * never during the parent's render.
   */
  getCoordinator?: () => SessionCoordinator;
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
  const { enabled, config, refresh, onLogout, epoch, getCoordinator } = options;

  const resolved = useMemo(() => {
    const base = resolveIdleConfig(config);
    return { ...base, epoch: typeof epoch === 'number' ? epoch : 0 };
  }, [config, epoch]);
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
  // The shared coordinator (or a hook-local one). All refresh/logout ordering
  // lives here so every entry point converges on the same "logout wins, cookie
  // clear last" guarantee.
  const coordinatorRef = useRef<SessionCoordinator | null>(null);

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
    // Ensure the controller latches its terminal generation so any pending or
    // future extend/deadline from this or another tab cannot revive the session.
    controllerRef.current?.logout();
    // Route the cookie clear through the coordinator so it waits for any
    // in-flight refresh and clears cookies last.
    void coordinatorRef.current?.logout();
  }, []);

  useEffect(() => {
    if (!enabled) return;

    const controller = createIdleController(resolved);
    controllerRef.current = controller;
    loggingOutRef.current = false;

    // Build (or adopt) the coordinator that serializes refresh vs logout.
    const localCoordinator =
      getCoordinator?.() ??
      createSessionCoordinator({
        refresh: () => refreshRef.current(),
        logout: () => onLogoutRef.current(),
      });
    coordinatorRef.current = localCoordinator;

    controller.subscribe((next) => {
      setPhase(next);
      if (next === 'expired') beginLogout();
    });
    // A peer logout (BroadcastChannel/storage) OR an adopted terminal record on
    // startup converges this tab too — delivered through the controller's
    // logout listener (Blocker 1: startup notifies the hook).
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
      coordinatorRef.current = null;
    };
  }, [enabled, resolved, beginLogout, getCoordinator]);

  const onStay = useCallback(() => {
    const controller = controllerRef.current;
    const activeCoordinator = coordinatorRef.current;
    if (!controller || !activeCoordinator || busy || loggingOutRef.current) return;
    setBusy(true);
    setErrorMessage(undefined);
    // Capture the session generation at the moment we start the refresh. If a
    // logout (local expiry, this tab's sign-out, or a remote tab) advances the
    // generation while the refresh is in flight, the refresh result is stale and
    // must NOT extend or restore the session.
    const startGeneration = controller.generation();
    void activeCoordinator
      .refresh()
      .then((ok) => {
        const stillOurSession =
          !loggingOutRef.current &&
          !controller.isLoggedOut() &&
          !activeCoordinator.isLoggedOut() &&
          controller.generation() === startGeneration;
        if (ok && stillOurSession) {
          // Confirm recent activity and reset the shared deadline for all tabs.
          controller.extend();
          setPhase('active');
          setRemainingMs(controller.remainingMs());
        } else if (!ok && !loggingOutRef.current) {
          // The coordinator already drove the logout on a failed refresh; mirror
          // the terminal UI state here.
          setErrorMessage(REFRESH_FAILED_MESSAGE);
          beginLogout();
        }
        // If logout already won, do nothing: the coordinator discards the late
        // response and clears cookies last.
      })
      .finally(() => {
        setBusy(false);
      });
  }, [busy, beginLogout]);

  const onSignOut = useCallback(() => {
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
