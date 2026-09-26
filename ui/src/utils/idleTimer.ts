/**
 * Idle-session timing and cross-tab coordination (#310).
 *
 * This module is a user-experience and local-exposure control, NOT an
 * authorization boundary. Protected APIs still verify tokens on every request
 * and the server-side absolute-lifetime policy from #309 remains authoritative.
 *
 * The controller derives its phase from an *absolute* deadline compared against
 * `Date.now()`, never from a decrement-only counter. A background tab whose
 * interval is throttled therefore cannot extend the session: when it next
 * evaluates the phase the wall-clock deadline has already passed.
 */

export interface IdleSessionConfig {
  idleTimeoutSeconds?: number;
  idleWarningSeconds?: number;
}

export interface ResolvedIdleConfig {
  idleTimeoutMs: number;
  idleWarningMs: number;
}

export type IdlePhase = 'active' | 'warning' | 'expired';

// Documented defaults from the issue: 30-minute idle timeout, 2-minute warning.
const DEFAULT_IDLE_TIMEOUT_SECONDS = 1800;
const DEFAULT_IDLE_WARNING_SECONDS = 120;

// Validated ranges (mirrored in ui/src/pages/api/config.ts and CloudFormation).
export const MIN_IDLE_TIMEOUT_SECONDS = 300; // 5 minutes
export const MAX_IDLE_TIMEOUT_SECONDS = 28800; // 8 hours
export const MIN_IDLE_WARNING_SECONDS = 30;
export const MAX_IDLE_WARNING_SECONDS = 600; // 10 minutes

// Recompute the deadline at most this often so a burst of pointer/keyboard
// events does not thrash the deadline or the cross-tab channel.
export const IDLE_ACTIVITY_THROTTLE_MS = 5_000;

const CHANNEL_NAME = 'game-agent-idle';
const DEADLINE_KEY = 'game-agent-idle-deadline';

function bounded(value: number | undefined, fallback: number, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return fallback;
  return Math.min(max, Math.max(min, Math.floor(value)));
}

export function resolveIdleConfig(config: IdleSessionConfig | undefined): ResolvedIdleConfig {
  const timeoutSeconds = bounded(
    config?.idleTimeoutSeconds,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    MIN_IDLE_TIMEOUT_SECONDS,
    MAX_IDLE_TIMEOUT_SECONDS,
  );
  let warningSeconds = bounded(
    config?.idleWarningSeconds,
    DEFAULT_IDLE_WARNING_SECONDS,
    MIN_IDLE_WARNING_SECONDS,
    MAX_IDLE_WARNING_SECONDS,
  );
  // The warning must open strictly before the timeout. If misconfigured larger
  // than (or equal to) the timeout, keep it at half the timeout so the dialog
  // still has a meaningful countdown window.
  if (warningSeconds >= timeoutSeconds) {
    warningSeconds = Math.max(1, Math.floor(timeoutSeconds / 2));
  }
  return {
    idleTimeoutMs: timeoutSeconds * 1000,
    idleWarningMs: warningSeconds * 1000,
  };
}

type PhaseListener = (phase: IdlePhase) => void;
type LogoutListener = () => void;

interface ChannelMessage {
  type: 'deadline' | 'logout';
  deadline?: number;
}

export interface IdleController {
  start(): void;
  stop(): void;
  tick(): void;
  phase(): IdlePhase;
  remainingMs(): number;
  deadline(): number;
  markActivity(): void;
  extend(): void;
  logout(): void;
  subscribe(listener: PhaseListener): () => void;
  onLogout(listener: LogoutListener): () => void;
}

// Guards against duplicate work when several React effects mount at once within
// a single tab; the cross-tab guard is the BroadcastChannel itself.
let activeControllerCount = 0;

export function createIdleController(config: ResolvedIdleConfig): IdleController {
  const phaseListeners = new Set<PhaseListener>();
  const logoutListeners = new Set<LogoutListener>();

  let deadlineMs = Date.now() + config.idleTimeoutMs;
  let lastActivityRecordedAt = 0;
  let lastPhase: IdlePhase = 'active';
  let channel: BroadcastChannel | null = null;
  let started = false;

  function computePhase(): IdlePhase {
    const remaining = deadlineMs - Date.now();
    if (remaining <= 0) return 'expired';
    if (remaining <= config.idleWarningMs) return 'warning';
    return 'active';
  }

  function persistDeadline(): void {
    try {
      window.localStorage.setItem(DEADLINE_KEY, String(deadlineMs));
    } catch {
      // Coordination degrades gracefully to per-tab timers when storage is off.
    }
  }

  function adoptDeadline(next: number): void {
    if (!Number.isFinite(next) || next <= 0) return;
    // Converge on the *latest* deadline any tab has established so a single
    // active tab keeps every tab signed in, but never move a deadline backwards.
    if (next > deadlineMs) {
      deadlineMs = next;
      emitPhaseIfChanged();
    }
  }

  function broadcast(message: ChannelMessage): void {
    if (channel) {
      try {
        channel.postMessage(message);
      } catch {
        // A closed channel simply means no peers to notify.
      }
    }
  }

  function emitPhaseIfChanged(): void {
    const phase = computePhase();
    if (phase !== lastPhase) {
      lastPhase = phase;
      for (const listener of phaseListeners) listener(phase);
    }
  }

  function setDeadline(next: number, propagate: boolean): void {
    deadlineMs = next;
    persistDeadline();
    if (propagate) broadcast({ type: 'deadline', deadline: next });
    emitPhaseIfChanged();
  }

  return {
    start(): void {
      if (started) return;
      started = true;
      activeControllerCount += 1;
      // Adopt any deadline another tab already published so tabs converge.
      try {
        const stored = Number(window.localStorage.getItem(DEADLINE_KEY));
        if (Number.isFinite(stored) && stored > Date.now()) {
          deadlineMs = Math.max(deadlineMs, stored);
        }
      } catch {
        // Ignore storage access failures.
      }
      lastActivityRecordedAt = Date.now();
      lastPhase = computePhase();
      if (typeof BroadcastChannel !== 'undefined') {
        channel = new BroadcastChannel(CHANNEL_NAME);
        channel.onmessage = (event: MessageEvent<ChannelMessage>) => {
          const data = event.data;
          if (!data) return;
          if (data.type === 'deadline' && typeof data.deadline === 'number') {
            adoptDeadline(data.deadline);
          } else if (data.type === 'logout') {
            for (const listener of logoutListeners) listener();
          }
        };
      }
      persistDeadline();
    },

    stop(): void {
      if (!started) return;
      started = false;
      activeControllerCount = Math.max(0, activeControllerCount - 1);
      if (channel) {
        channel.close();
        channel = null;
      }
    },

    tick(): void {
      emitPhaseIfChanged();
    },

    phase(): IdlePhase {
      return computePhase();
    },

    remainingMs(): number {
      return Math.max(0, deadlineMs - Date.now());
    },

    deadline(): number {
      return deadlineMs;
    },

    markActivity(): void {
      const now = Date.now();
      // Only meaningful, throttled activity resets the deadline, and only while
      // the session is still active. Activity during the warning must go through
      // the explicit "Stay signed in" action (extend), never passive events.
      if (computePhase() !== 'active') return;
      if (now - lastActivityRecordedAt < IDLE_ACTIVITY_THROTTLE_MS) return;
      lastActivityRecordedAt = now;
      setDeadline(now + config.idleTimeoutMs, true);
    },

    extend(): void {
      const now = Date.now();
      lastActivityRecordedAt = now;
      setDeadline(now + config.idleTimeoutMs, true);
    },

    logout(): void {
      broadcast({ type: 'logout' });
      for (const listener of logoutListeners) listener();
    },

    subscribe(listener: PhaseListener): () => void {
      phaseListeners.add(listener);
      return () => phaseListeners.delete(listener);
    },

    onLogout(listener: LogoutListener): () => void {
      logoutListeners.add(listener);
      return () => logoutListeners.delete(listener);
    },
  };
}

export function resetIdleCoordinatorForTests(): void {
  activeControllerCount = 0;
  try {
    window.localStorage.removeItem(DEADLINE_KEY);
  } catch {
    // Ignore storage access failures in test teardown.
  }
}
