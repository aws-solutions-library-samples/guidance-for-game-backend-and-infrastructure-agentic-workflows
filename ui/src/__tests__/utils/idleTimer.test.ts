import {
  resolveIdleConfig,
  createIdleController,
  resetIdleCoordinatorForTests,
  allocateSessionEpoch,
  IDLE_ACTIVITY_THROTTLE_MS,
} from '@/utils/idleTimer';

type ChannelData = { type?: string; deadline?: number; generation?: number };

// A queued BroadcastChannel mock. The real BroadcastChannel delivers messages
// asynchronously via the event loop; a synchronous mock hides ordering races
// (a listener can observe a message before the poster finishes its own work).
// Delivery is buffered and only flushed by an explicit `flush()` so tests can
// assert behavior against controlled message ordering.
class MockBroadcastChannel {
  static instances: MockBroadcastChannel[] = [];
  static queue: Array<() => void> = [];
  onmessage: ((event: MessageEvent<ChannelData>) => void) | null = null;
  postMessage = jest.fn((message: ChannelData) => {
    for (const other of MockBroadcastChannel.instances) {
      if (other !== this && other.name === this.name) {
        MockBroadcastChannel.queue.push(() => {
          other.onmessage?.({ data: message } as MessageEvent<ChannelData>);
        });
      }
    }
  });
  close = jest.fn(() => {
    MockBroadcastChannel.instances = MockBroadcastChannel.instances.filter((c) => c !== this);
  });

  constructor(public name: string) {
    MockBroadcastChannel.instances.push(this);
  }

  static flush(): void {
    const pending = MockBroadcastChannel.queue;
    MockBroadcastChannel.queue = [];
    for (const deliver of pending) deliver();
  }
}

describe('resolveIdleConfig', () => {
  it('applies documented defaults when values are missing', () => {
    expect(resolveIdleConfig(undefined)).toEqual({
      idleTimeoutMs: 1800 * 1000,
      idleWarningMs: 120 * 1000,
    });
  });

  it('keeps the warning threshold strictly inside the idle timeout', () => {
    // A warning threshold at or above the timeout would open the dialog immediately.
    const config = resolveIdleConfig({ idleTimeoutSeconds: 300, idleWarningSeconds: 600 });
    expect(config.idleWarningMs).toBeLessThan(config.idleTimeoutMs);
    expect(config.idleWarningMs).toBeGreaterThan(0);
  });

  it('rejects non-finite input and falls back to defaults', () => {
    const config = resolveIdleConfig({
      idleTimeoutSeconds: Number.NaN,
      idleWarningSeconds: Number.POSITIVE_INFINITY,
    });
    expect(config.idleTimeoutMs).toBe(1800 * 1000);
    expect(config.idleWarningMs).toBe(120 * 1000);
  });
});

describe('idle controller', () => {
  let now: number;

  beforeEach(() => {
    resetIdleCoordinatorForTests();
    window.localStorage.clear();
    MockBroadcastChannel.instances = [];
    MockBroadcastChannel.queue = [];
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: MockBroadcastChannel,
    });
    now = 1_000_000;
    jest.spyOn(Date, 'now').mockImplementation(() => now);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  const config = { idleTimeoutMs: 30 * 60_000, idleWarningMs: 2 * 60_000 };

  it('starts active and reports the phase from the absolute deadline', () => {
    const controller = createIdleController(config);
    controller.start();
    expect(controller.phase()).toBe('active');

    // Advance close to the warning threshold (28 minutes).
    now += 28 * 60_000 - 1;
    expect(controller.phase()).toBe('active');

    // Cross the warning threshold.
    now += 2;
    expect(controller.phase()).toBe('warning');
    controller.stop();
  });

  it('reaches the expired phase exactly at the absolute deadline', () => {
    const controller = createIdleController(config);
    controller.start();
    now += 30 * 60_000;
    expect(controller.phase()).toBe('expired');
    controller.stop();
  });

  it('is resilient to background-tab throttling: a long jump still expires', () => {
    // Simulate a background tab whose interval never fired; when it wakes the
    // absolute deadline has already passed, so the phase is expired, not "still
    // counting down from where a decrement-only timer left off".
    const controller = createIdleController(config);
    controller.start();
    now += 45 * 60_000; // way past the 30-minute deadline
    expect(controller.phase()).toBe('expired');
    expect(controller.remainingMs()).toBe(0);
    controller.stop();
  });

  it('meaningful activity before the warning resets the deadline', () => {
    const controller = createIdleController(config);
    controller.start();
    now += 20 * 60_000;
    controller.markActivity();
    // Deadline moved forward; 20 minutes later we are still active (total 40m
    // wall clock but only 20m since the reset).
    now += 20 * 60_000;
    expect(controller.phase()).toBe('active');
    controller.stop();
  });

  it('throttles rapid activity so the deadline is not recomputed every event', () => {
    const controller = createIdleController(config);
    controller.start();
    const firstDeadline = controller.deadline();
    now += 10 * 60_000;
    controller.markActivity();
    const secondDeadline = controller.deadline();
    expect(secondDeadline).toBeGreaterThan(firstDeadline);

    // A second activity within the throttle window must not move the deadline.
    now += IDLE_ACTIVITY_THROTTLE_MS - 1;
    controller.markActivity();
    expect(controller.deadline()).toBe(secondDeadline);
    controller.stop();
  });

  it('notifies phase-change subscribers on tick', () => {
    const controller = createIdleController(config);
    const onPhase = jest.fn();
    controller.subscribe(onPhase);
    controller.start();
    now += 29 * 60_000;
    controller.tick();
    expect(onPhase).toHaveBeenCalledWith('warning');
    now += 1 * 60_000;
    controller.tick();
    expect(onPhase).toHaveBeenCalledWith('expired');
    controller.stop();
  });

  it('extend() pushes a fresh deadline and broadcasts it to other tabs', () => {
    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    tabA.start();
    tabB.start();

    now += 29 * 60_000; // both in warning
    tabA.tick();
    tabB.tick();
    expect(tabA.phase()).toBe('warning');
    expect(tabB.phase()).toBe('warning');

    tabA.extend();
    MockBroadcastChannel.flush();
    // Tab B converged on the extended deadline without its own user action.
    expect(tabB.phase()).toBe('active');
    tabA.stop();
    tabB.stop();
  });

  it('broadcasts logout so other tabs converge on expiry', () => {
    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    const onExpiredB = jest.fn();
    tabA.start();
    tabB.start();
    tabB.onLogout(onExpiredB);

    tabA.logout();
    MockBroadcastChannel.flush();
    expect(onExpiredB).toHaveBeenCalledTimes(1);
    tabA.stop();
    tabB.stop();
  });

  // ---- Blocker 2 regressions: cross-tab convergence via versioned state ----

  it('a later-starting tab broadcasts its newer deadline so existing tabs converge', () => {
    // Tab A starts now with a 30-minute deadline.
    const tabA = createIdleController(config);
    tabA.start();
    const deadlineA = tabA.deadline();

    // 10 minutes later Tab B starts. Its fresh deadline is strictly newer.
    now += 10 * 60_000;
    const tabB = createIdleController(config);
    tabB.start();
    MockBroadcastChannel.flush();

    // Tab A must have adopted Tab B's newer deadline (it was broadcast), not
    // kept its own older one. Previously B persisted without broadcasting and A
    // would expire while B still reported active.
    expect(tabA.deadline()).toBeGreaterThan(deadlineA);
    expect(tabA.deadline()).toBe(tabB.deadline());
    tabA.stop();
    tabB.stop();
  });

  it('a new tab does not silently extend an already-established session past a peer deadline', () => {
    // Tab A has been active and just refreshed: its deadline is now+30m.
    const tabA = createIdleController(config);
    tabA.start();
    now += 5 * 60_000;
    tabA.extend(); // deadline = now + 30m
    MockBroadcastChannel.flush();
    const established = tabA.deadline();

    // A brand-new tab starts. It must adopt the established (later) shared
    // deadline rather than resetting everyone to its own now+30m — but since
    // both compute the same now+30m here, the invariant we assert is that the
    // established peer deadline is never moved *backward* and both converge.
    const tabB = createIdleController(config);
    tabB.start();
    MockBroadcastChannel.flush();
    expect(tabB.deadline()).toBeGreaterThanOrEqual(established);
    expect(tabA.deadline()).toBe(tabB.deadline());
    tabA.stop();
    tabB.stop();
  });

  it('automatic expiry (not just explicit logout) broadcasts logout to other tabs', () => {
    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    const onLogoutB = jest.fn();
    tabA.start();
    tabB.start();
    tabB.onLogout(onLogoutB);

    // Tab A crosses the absolute deadline and evaluates its phase on tick.
    now += 31 * 60_000;
    tabA.tick();
    MockBroadcastChannel.flush();

    // The other tab must be told to log out, not left showing authenticated UI.
    expect(onLogoutB).toHaveBeenCalledTimes(1);
    tabA.stop();
    tabB.stop();
  });

  // ---- Blocker 2 regression: an elapsed persisted deadline is consumed on
  // startup as terminal logout, never revived with a fresh constructor deadline. ----

  it('consumes an elapsed persisted deadline on startup as logout instead of granting a fresh timeout', () => {
    // A prior tab established a deadline for THIS epoch, then every tab slept or
    // closed before a tick could record terminal logout.
    const prior = createIdleController(config);
    prior.start();
    const priorDeadline = prior.deadline();
    prior.stop();

    // Wall clock advances PAST that persisted deadline while no tab was ticking.
    now = priorDeadline + 60_000;

    // Reopening from the still-active (but elapsed) persisted record must treat
    // it as terminal, not publish a fresh now+timeout deadline.
    const reopened = createIdleController(config);
    const onLogout = jest.fn();
    reopened.onLogout(onLogout);
    reopened.start();

    expect(reopened.isLoggedOut()).toBe(true);
    expect(reopened.phase()).toBe('expired');
    expect(reopened.remainingMs()).toBe(0);
    // Startup must notify the hook so the reopened tab signs out.
    expect(onLogout).toHaveBeenCalledTimes(1);
    // The persisted record is now terminal, not a revived future deadline.
    const record = JSON.parse(window.localStorage.getItem('game-agent-idle-session') as string);
    expect(record.loggedOut).toBe(true);
    reopened.stop();
  });

  it('an elapsed-deadline startup broadcasts terminal logout so already-open peers converge', () => {
    // Tab A is already open and mid-session.
    const tabA = createIdleController(config);
    tabA.start();
    const onLogoutA = jest.fn();
    tabA.onLogout(onLogoutA);
    const deadlineA = tabA.deadline();

    // Wall clock passes the shared deadline (both tabs were throttled). A new
    // tab opens and observes the elapsed persisted record on startup.
    now = deadlineA + 30_000;
    const tabB = createIdleController(config);
    tabB.start();
    MockBroadcastChannel.flush();

    // The startup terminal transition must propagate to the already-open peer.
    expect(tabB.isLoggedOut()).toBe(true);
    expect(onLogoutA).toHaveBeenCalledTimes(1);
    tabA.stop();
    tabB.stop();
  });

  // ---- Blocker 1 regression: a persisted monotonic epoch allocator drives
  // fresh-session vs later-tab-convergence, independent of component state. ----

  describe('allocateSessionEpoch', () => {
    it('allocates epoch 1 when no session has ever been persisted', () => {
      expect(allocateSessionEpoch()).toBe(1);
    });

    it('allocates an epoch strictly past a terminal record so a fresh sign-in cannot reuse it', () => {
      // A prior session at epoch 1 logged out and persisted a terminal record.
      const prior = createIdleController({ ...config, epoch: 1 });
      prior.beginSession(); // adopts epoch 1 (one past stored 0)
      prior.start();
      prior.logout();
      MockBroadcastChannel.flush();
      prior.stop();

      // A brand-new sign-in (e.g. after a full reload) must NOT reuse epoch 1.
      const next = allocateSessionEpoch();
      expect(next).toBeGreaterThan(1);
    });

    it('adopts the epoch of a LIVE (non-terminal) session so a later tab converges instead of splitting', () => {
      // An active session exists at some epoch.
      const active = createIdleController(config);
      active.beginSession();
      active.start();
      MockBroadcastChannel.flush();
      const liveEpoch = active.epoch();

      // A later tab in the SAME authenticated session must adopt liveEpoch, not
      // allocate a new one that would split cross-tab coordination.
      expect(allocateSessionEpoch()).toBe(liveEpoch);
      active.stop();
    });
  });

  it('persists a terminal session record on logout so a new tab does not resurrect the session', () => {
    const tabA = createIdleController(config);
    tabA.start();
    tabA.logout();
    MockBroadcastChannel.flush();

    // A single versioned record carries the terminal state; a tab starting
    // afterward at the same epoch adopts the logout instead of a stale deadline.
    const raw = window.localStorage.getItem('game-agent-idle-session');
    expect(raw).not.toBeNull();
    const record = JSON.parse(raw as string);
    expect(record.loggedOut).toBe(true);

    // A tab starting at the same epoch converges to logged-out.
    const tabB = createIdleController(config);
    const onLogoutB = jest.fn();
    tabB.onLogout(onLogoutB);
    tabB.start();
    MockBroadcastChannel.flush();
    expect(tabB.isLoggedOut()).toBe(true);
    // Startup must NOTIFY the hook (Blocker 1): a tab opened after peer logout
    // does not silently keep authenticated UI.
    expect(onLogoutB).toHaveBeenCalledTimes(1);
    tabA.stop();
    tabB.stop();
  });

  // ---- Blocker 1 regressions: session epoch binds startup + reauthentication ----

  it('startup after peer logout notifies the hook exactly once', () => {
    const tabA = createIdleController(config);
    tabA.start();
    tabA.logout();
    MockBroadcastChannel.flush();
    tabA.stop();

    // A brand-new controller opened afterward (same epoch) must deliver the
    // adopted terminal state to its logout listener, not just latch internally.
    const tabB = createIdleController(config);
    const onLogoutB = jest.fn();
    tabB.onLogout(onLogoutB);
    tabB.start();
    MockBroadcastChannel.flush();
    expect(tabB.phase()).toBe('expired');
    expect(onLogoutB).toHaveBeenCalledTimes(1);
    tabB.stop();
  });

  it('beginSession() supersedes a terminal record so a later sign-in is not immediately logged out', () => {
    // A prior session logged out and persisted its terminal record.
    const prior = createIdleController(config);
    prior.start();
    prior.logout();
    MockBroadcastChannel.flush();
    prior.stop();

    // The user signs in again: a fresh epoch is explicitly initialized.
    const next = createIdleController(config);
    const onLogoutNext = jest.fn();
    next.onLogout(onLogoutNext);
    next.beginSession();
    next.start();
    MockBroadcastChannel.flush();

    // The new session must be active, not immediately expired.
    expect(next.isLoggedOut()).toBe(false);
    expect(next.phase()).toBe('active');
    expect(onLogoutNext).not.toHaveBeenCalled();
    next.stop();
  });

  it('a stored terminal record from an OLDER epoch does not log out a newer session', () => {
    const prior = createIdleController(config);
    prior.start();
    prior.logout();
    MockBroadcastChannel.flush();
    prior.stop();

    // A new authenticated epoch begins. A stored terminal record from the old
    // epoch must be ignored (it belongs to a superseded session).
    const next = createIdleController(config);
    next.beginSession();
    next.start();
    MockBroadcastChannel.flush();
    expect(next.isLoggedOut()).toBe(false);

    // The persisted record now belongs to the new (active) epoch.
    const record = JSON.parse(window.localStorage.getItem('game-agent-idle-session') as string);
    expect(record.loggedOut).toBe(false);
    next.stop();
  });

  it('rejects a stale deadline broadcast from an older generation after logout', () => {
    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    const onLogoutA = jest.fn();
    tabA.start();
    tabB.start();
    tabA.onLogout(onLogoutA);

    // Tab B logs out (terminal). Tab A converges to logged-out.
    tabB.logout();
    MockBroadcastChannel.flush();
    expect(onLogoutA).toHaveBeenCalledTimes(1);

    // A late deadline message from the pre-logout generation must NOT revive A.
    tabB.extend();
    MockBroadcastChannel.flush();
    expect(tabA.phase()).toBe('expired');
    tabA.stop();
    tabB.stop();
  });

  it('converges via localStorage when BroadcastChannel is unavailable', () => {
    // Simulate an environment without BroadcastChannel.
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: undefined,
    });

    const tabA = createIdleController(config);
    tabA.start();
    now += 3 * 60_000;
    tabA.extend(); // persists a newer deadline to localStorage
    const established = tabA.deadline();

    // A second tab starting later reads the persisted deadline and adopts the
    // later of (its fresh deadline, stored deadline).
    const tabB = createIdleController(config);
    tabB.start();
    expect(tabB.deadline()).toBeGreaterThanOrEqual(established);
    tabA.stop();
    tabB.stop();
  });

  // ---- Blocker 2: live storage-event convergence for ALREADY-OPEN tabs ----

  // Deliver a localStorage write to every other open controller as a real
  // `storage` event would (the writing tab never receives its own event).
  function dispatchStorageFromWrite(key: string): void {
    const newValue = window.localStorage.getItem(key);
    window.dispatchEvent(
      new StorageEvent('storage', {
        key,
        newValue,
        storageArea: window.localStorage,
      }),
    );
  }

  it('without BroadcastChannel, an already-open tab converges on a peer deadline extension via storage', () => {
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: undefined,
    });

    // BOTH tabs are already open (the exact case a startup-only snapshot misses).
    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    tabA.start();
    tabB.start();

    // Push both into the warning window.
    now += 29 * 60_000;
    tabA.tick();
    tabB.tick();
    expect(tabB.phase()).toBe('warning');

    // Tab A extends. In a real browser this writes localStorage and every other
    // open tab receives a `storage` event asynchronously.
    tabA.extend();
    dispatchStorageFromWrite('game-agent-idle-session');

    // The already-open Tab B must have converged on the extended deadline.
    expect(tabB.phase()).toBe('active');
    expect(tabB.deadline()).toBe(tabA.deadline());
    tabA.stop();
    tabB.stop();
  });

  it('without BroadcastChannel, an already-open tab converges on a peer logout via storage', () => {
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: undefined,
    });

    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    const onLogoutB = jest.fn();
    tabA.start();
    tabB.start();
    tabB.onLogout(onLogoutB);

    // Tab A logs out; Tab B (already open) must observe it through storage.
    tabA.logout();
    dispatchStorageFromWrite('game-agent-idle-session');

    expect(onLogoutB).toHaveBeenCalledTimes(1);
    expect(tabB.phase()).toBe('expired');
    tabA.stop();
    tabB.stop();
  });

  it('a storage event from an older generation does not revive a logged-out tab', () => {
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: undefined,
    });

    const tabA = createIdleController(config);
    const tabB = createIdleController(config);
    tabA.start();
    tabB.start();

    // Tab B logs out.
    tabB.logout();
    dispatchStorageFromWrite('game-agent-idle-session');
    expect(tabB.phase()).toBe('expired');

    // A stale deadline record from a pre-logout generation must not revive B.
    // Simulate it by writing a record with a lower generation.
    const stale = JSON.stringify({
      epoch: tabA.epoch ? tabA.epoch() : 0,
      generation: 0,
      deadline: now + 30 * 60_000,
      loggedOut: false,
    });
    window.localStorage.setItem('game-agent-idle-session', stale);
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'game-agent-idle-session',
        newValue: stale,
        storageArea: window.localStorage,
      }),
    );
    expect(tabB.phase()).toBe('expired');
    tabA.stop();
    tabB.stop();
  });

  it('removes the storage listener on stop()', () => {
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: undefined,
    });
    const removeSpy = jest.spyOn(window, 'removeEventListener');
    const controller = createIdleController(config);
    controller.start();
    controller.stop();
    expect(removeSpy).toHaveBeenCalledWith('storage', expect.any(Function));
    removeSpy.mockRestore();
  });
});
