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
const GENERATION_KEY = 'game-agent-idle-generation';
const LOGOUT_KEY = 'game-agent-idle-loggedout';

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
  // Versioned shared state: every message carries the generation it belongs to.
  // A logout bumps the generation to a terminal value; peers reject any deadline
  // message from an older generation so a late refresh cannot revive a session.
  generation: number;
}

export interface IdleController {
  start(): void;
  stop(): void;
  tick(): void;
  phase(): IdlePhase;
  remainingMs(): number;
  deadline(): number;
  generation(): number;
  isLoggedOut(): boolean;
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
  // Session-transition version. Any extend/deadline change increments it within
  // the active session; logout advances it to a terminal generation and latches
  // `loggedOut`. Once logged out the controller ignores extensions and rejects
  // deadline messages from older generations — this is what makes "logout wins".
  let generation = 0;
  let loggedOut = false;

  function computePhase(): IdlePhase {
    if (loggedOut) return 'expired';
    const remaining = deadlineMs - Date.now();
    if (remaining <= 0) return 'expired';
    if (remaining <= config.idleWarningMs) return 'warning';
    return 'active';
  }

  function readStoredNumber(key: string): number | null {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return null;
      const value = Number(raw);
      return Number.isFinite(value) ? value : null;
    } catch {
      return null;
    }
  }

  function persistState(): void {
    try {
      window.localStorage.setItem(DEADLINE_KEY, String(deadlineMs));
      window.localStorage.setItem(GENERATION_KEY, String(generation));
    } catch {
      // Coordination degrades gracefully to per-tab timers when storage is off.
    }
  }

  function persistLogout(): void {
    try {
      window.localStorage.setItem(GENERATION_KEY, String(generation));
      window.localStorage.setItem(LOGOUT_KEY, String(generation));
      // Clear stale deadline state so a newly opened tab does not resurrect the
      // session from a pre-logout deadline.
      window.localStorage.removeItem(DEADLINE_KEY);
    } catch {
      // Nothing more we can do; the terminal state is still authoritative here.
    }
  }

  function notifyLogout(): void {
    for (const listener of logoutListeners) listener();
  }

  // Latch the terminal logged-out state exactly once. `propagate` broadcasts the
  // terminal generation to peers (used for both explicit logout and automatic
  // expiry); a message received from a peer converges without re-broadcasting.
  function enterLoggedOut(nextGeneration: number, propagate: boolean): void {
    if (loggedOut && nextGeneration <= generation) {
      return;
    }
    loggedOut = true;
    generation = Math.max(generation, nextGeneration);
    persistLogout();
    if (propagate) broadcast({ type: 'logout', generation });
    emitPhaseIfChanged();
    notifyLogout();
  }

  function adoptDeadline(next: number, incomingGeneration: number): void {
    if (loggedOut) return; // A terminated session is never revived by a deadline.
    if (!Number.isFinite(next) || next <= 0) return;
    // Reject messages from an older generation (e.g. a late extension racing a
    // newer logout/extend).
    if (incomingGeneration < generation) return;
    if (incomingGeneration > generation) {
      generation = incomingGeneration;
    }
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
    if (loggedOut) return;
    deadlineMs = next;
    generation += 1;
    persistState();
    if (propagate) broadcast({ type: 'deadline', deadline: next, generation });
    emitPhaseIfChanged();
  }

  // Automatic expiry: when the phase reaches `expired` on its own (deadline
  // passed) we must converge peers too, not only on explicit logout.
  function handleAutomaticExpiryIfNeeded(): void {
    if (loggedOut) return;
    if (Date.now() >= deadlineMs) {
      enterLoggedOut(generation + 1, true);
    }
  }

  return {
    start(): void {
      if (started) return;
      started = true;
      activeControllerCount += 1;

      // Adopt any shared state another tab already published so tabs converge.
      const storedGeneration = readStoredNumber(GENERATION_KEY);
      const storedLogout = readStoredNumber(LOGOUT_KEY);
      const storedDeadline = readStoredNumber(DEADLINE_KEY);
      if (typeof storedGeneration === 'number') {
        generation = Math.max(generation, storedGeneration);
      }
      // If a peer already logged out at our generation-or-newer, converge to the
      // terminal state instead of starting a fresh authenticated timer.
      if (typeof storedLogout === 'number' && storedLogout >= generation) {
        generation = Math.max(generation, storedLogout);
        loggedOut = true;
      }
      if (!loggedOut && typeof storedDeadline === 'number' && storedDeadline > Date.now()) {
        deadlineMs = Math.max(deadlineMs, storedDeadline);
      }

      lastActivityRecordedAt = Date.now();
      lastPhase = computePhase();

      if (typeof BroadcastChannel !== 'undefined') {
        channel = new BroadcastChannel(CHANNEL_NAME);
        channel.onmessage = (event: MessageEvent<ChannelMessage>) => {
          const data = event.data;
          if (!data) return;
          if (data.type === 'deadline' && typeof data.deadline === 'number') {
            adoptDeadline(data.deadline, typeof data.generation === 'number' ? data.generation : 0);
          } else if (data.type === 'logout') {
            enterLoggedOut(typeof data.generation === 'number' ? data.generation : generation + 1, false);
          }
        };
      }

      if (loggedOut) {
        // Converge asynchronously so subscribers attached after start() still fire.
        persistLogout();
      } else {
        // Publish our (possibly newest) deadline so existing tabs converge on a
        // later-starting tab's deadline instead of expiring under it.
        persistState();
        broadcast({ type: 'deadline', deadline: deadlineMs, generation });
      }
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
      handleAutomaticExpiryIfNeeded();
      emitPhaseIfChanged();
    },

    phase(): IdlePhase {
      return computePhase();
    },

    remainingMs(): number {
      if (loggedOut) return 0;
      return Math.max(0, deadlineMs - Date.now());
    },

    deadline(): number {
      return deadlineMs;
    },

    generation(): number {
      return generation;
    },

    isLoggedOut(): boolean {
      return loggedOut;
    },

    markActivity(): void {
      if (loggedOut) return;
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
      if (loggedOut) return; // A won logout is never reversed by a later extend.
      const now = Date.now();
      lastActivityRecordedAt = now;
      setDeadline(now + config.idleTimeoutMs, true);
    },

    logout(): void {
      enterLoggedOut(generation + 1, true);
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
    window.localStorage.removeItem(GENERATION_KEY);
    window.localStorage.removeItem(LOGOUT_KEY);
  } catch {
    // Ignore storage access failures in test teardown.
  }
}
