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
 *
 * Cross-tab state is a SINGLE versioned session record bound to an authenticated
 * session `epoch` (Blocker 1). One record — not three separate keys — carries
 * `{ epoch, generation, deadline, loggedOut }`, so a newly opened tab never sees
 * an intermediate mix of a stale deadline and a fresh generation. A terminal
 * (logged-out) record is consumed on startup and delivered to the hook so a tab
 * opened after a peer logout does not keep authenticated UI. `beginSession()`
 * explicitly initializes a fresh epoch after authentication, so the terminal
 * record from a prior session does not immediately log the new session out.
 *
 * When `BroadcastChannel` is unavailable the controller registers a live
 * `storage` listener (Blocker 2) so already-open tabs converge on a peer's
 * deadline extension or logout, applying the same epoch/generation validation.
 */

export interface IdleSessionConfig {
  idleTimeoutSeconds?: number;
  idleWarningSeconds?: number;
}

export interface ResolvedIdleConfig {
  idleTimeoutMs: number;
  idleWarningMs: number;
  /**
   * Authenticated session epoch. All persisted/broadcast state is bound to it;
   * a record from a different epoch is ignored. Defaults to 0 when unset (a
   * single-session environment). The hook derives this from authentication.
   */
  epoch?: number;
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
// A SINGLE versioned session record replaces the former separate deadline/
// generation/logout keys so tabs never observe an inconsistent intermediate.
const SESSION_KEY = 'game-agent-idle-session';

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
  // The authenticated session the message belongs to. Messages from a different
  // epoch are ignored.
  epoch: number;
}

interface SessionRecord {
  epoch: number;
  generation: number;
  deadline: number;
  loggedOut: boolean;
}

export interface IdleController {
  start(): void;
  stop(): void;
  tick(): void;
  phase(): IdlePhase;
  remainingMs(): number;
  deadline(): number;
  generation(): number;
  epoch(): number;
  isLoggedOut(): boolean;
  markActivity(): void;
  extend(): void;
  logout(): void;
  /**
   * Begin a fresh authenticated session: adopt a new epoch (one past any stored
   * epoch) so a terminal record from a prior session does not log this one out.
   * Call after authentication succeeds and BEFORE start().
   */
  beginSession(): void;
  subscribe(listener: PhaseListener): () => void;
  onLogout(listener: LogoutListener): () => void;
}

// Guards against duplicate work when several React effects mount at once within
// a single tab; the cross-tab guard is the BroadcastChannel/storage record.
let activeControllerCount = 0;

export function createIdleController(config: ResolvedIdleConfig): IdleController {
  const phaseListeners = new Set<PhaseListener>();
  const logoutListeners = new Set<LogoutListener>();

  let epoch = typeof config.epoch === 'number' && Number.isFinite(config.epoch) ? config.epoch : 0;
  let deadlineMs = Date.now() + config.idleTimeoutMs;
  let lastActivityRecordedAt = 0;
  let lastPhase: IdlePhase = 'active';
  let channel: BroadcastChannel | null = null;
  let storageListener: ((event: StorageEvent) => void) | null = null;
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

  function readRecord(): SessionRecord | null {
    try {
      const raw = window.localStorage.getItem(SESSION_KEY);
      if (raw === null) return null;
      const parsed = JSON.parse(raw) as Partial<SessionRecord>;
      if (
        typeof parsed?.epoch !== 'number' ||
        typeof parsed?.generation !== 'number' ||
        typeof parsed?.deadline !== 'number' ||
        typeof parsed?.loggedOut !== 'boolean'
      ) {
        return null;
      }
      return parsed as SessionRecord;
    } catch {
      return null;
    }
  }

  function writeRecord(): void {
    try {
      const record: SessionRecord = { epoch, generation, deadline: deadlineMs, loggedOut };
      window.localStorage.setItem(SESSION_KEY, JSON.stringify(record));
    } catch {
      // Coordination degrades gracefully to per-tab timers when storage is off.
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
    writeRecord();
    if (propagate) broadcast({ type: 'logout', generation, epoch });
    emitPhaseIfChanged();
    notifyLogout();
  }

  function adoptDeadline(next: number, incomingGeneration: number, incomingEpoch: number): void {
    if (incomingEpoch !== epoch) return; // A message from another session is ignored.
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

  // Apply an incoming record (from a BroadcastChannel message or a live storage
  // event) with the same epoch/generation validation.
  function applyIncomingRecord(record: SessionRecord): void {
    if (record.epoch !== epoch) return;
    if (record.loggedOut) {
      enterLoggedOut(record.generation, false);
      return;
    }
    adoptDeadline(record.deadline, record.generation, record.epoch);
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
    writeRecord();
    if (propagate) broadcast({ type: 'deadline', deadline: next, generation, epoch });
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

      // Adopt any shared state another tab already published for THIS epoch so
      // tabs converge. A record from a different epoch is a superseded session
      // and is ignored (Blocker 1: beginSession() must not be logged out by it).
      const stored = readRecord();
      let selfDetectedTerminal = false;
      if (stored && stored.epoch === epoch) {
        generation = Math.max(generation, stored.generation);
        if (stored.loggedOut) {
          loggedOut = true;
        } else if (stored.deadline > Date.now()) {
          deadlineMs = Math.max(deadlineMs, stored.deadline);
        } else {
          // Blocker 2: a same-epoch, non-terminal record whose deadline has
          // already elapsed (every tab slept/closed/throttled past it before a
          // tick recorded logout) must be CONSUMED as terminal on startup, not
          // replaced by a fresh now+timeout deadline. Advance the generation so
          // this transition is authoritative and broadcast it to peers below.
          loggedOut = true;
          generation = stored.generation + 1;
          selfDetectedTerminal = true;
        }
      }

      lastActivityRecordedAt = Date.now();
      lastPhase = computePhase();

      if (typeof BroadcastChannel !== 'undefined') {
        channel = new BroadcastChannel(CHANNEL_NAME);
        channel.onmessage = (event: MessageEvent<ChannelMessage>) => {
          const data = event.data;
          if (!data || typeof data.epoch !== 'number' || data.epoch !== epoch) return;
          if (data.type === 'deadline' && typeof data.deadline === 'number') {
            adoptDeadline(data.deadline, typeof data.generation === 'number' ? data.generation : 0, data.epoch);
          } else if (data.type === 'logout') {
            enterLoggedOut(typeof data.generation === 'number' ? data.generation : generation + 1, false);
          }
        };
      } else {
        // Blocker 2: without BroadcastChannel, a live storage listener converges
        // already-open tabs on a peer's extension/logout. The writing tab does
        // not receive its own event, so this only fires for peer writes.
        storageListener = (event: StorageEvent) => {
          if (event.key !== SESSION_KEY) return;
          const record = readRecord();
          if (record) applyIncomingRecord(record);
        };
        window.addEventListener('storage', storageListener);
      }

      if (loggedOut) {
        // Consume the adopted terminal state and DELIVER it to the hook so a tab
        // opened after peer logout signs out instead of keeping authenticated UI.
        writeRecord();
        // A terminal state we DETECTED ourselves (an elapsed persisted deadline,
        // Blocker 2) must be broadcast so already-open peers converge; a terminal
        // record ADOPTED from a peer is not re-broadcast (it already propagated).
        if (selfDetectedTerminal) {
          broadcast({ type: 'logout', generation, epoch });
        }
        notifyLogout();
      } else {
        // Publish our (possibly newest) deadline so existing tabs converge on a
        // later-starting tab's deadline instead of expiring under it.
        writeRecord();
        broadcast({ type: 'deadline', deadline: deadlineMs, generation, epoch });
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
      if (storageListener) {
        window.removeEventListener('storage', storageListener);
        storageListener = null;
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

    epoch(): number {
      return epoch;
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

    beginSession(): void {
      // Pick an epoch strictly newer than anything stored so a prior terminal
      // record cannot log this fresh session out.
      const stored = readRecord();
      const base = stored ? stored.epoch : epoch;
      epoch = Math.max(epoch, base) + 1;
      generation = 0;
      loggedOut = false;
      deadlineMs = Date.now() + config.idleTimeoutMs;
      lastPhase = 'active';
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
    window.localStorage.removeItem(SESSION_KEY);
  } catch {
    // Ignore storage access failures in test teardown.
  }
}

// Parse the single versioned session record from localStorage (module-level so
// the epoch allocator can read the same source of truth the controller writes).
function readPersistedSessionRecord(): SessionRecord | null {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<SessionRecord>;
    if (
      typeof parsed?.epoch !== 'number' ||
      typeof parsed?.generation !== 'number' ||
      typeof parsed?.deadline !== 'number' ||
      typeof parsed?.loggedOut !== 'boolean'
    ) {
      return null;
    }
    return parsed as SessionRecord;
  } catch {
    return null;
  }
}

/**
 * Allocate the authenticated session epoch to bind a newly authenticated tab to
 * (#310, Blocker 1). This is the ONE monotonic, persisted source of the epoch —
 * production must derive the epoch from here, never from component-local state
 * that resets to zero on every reload/new mount.
 *
 * - When a LIVE (non-terminal) session record exists, adopt its epoch so a later
 *   tab in the same authenticated session converges on the same cross-tab state
 *   instead of splitting under a different epoch.
 * - Otherwise (no record, or a terminal record from a prior session), allocate
 *   an epoch strictly past the stored one so a fresh sign-in — including one
 *   after a full reload — can never reuse a terminal epoch and be immediately
 *   logged out by its leftover record.
 */
export function allocateSessionEpoch(): number {
  const stored = readPersistedSessionRecord();
  if (stored && !stored.loggedOut) {
    return stored.epoch;
  }
  return (stored ? stored.epoch : 0) + 1;
}
