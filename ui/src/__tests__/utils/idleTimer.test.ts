import {
  resolveIdleConfig,
  createIdleController,
  resetIdleCoordinatorForTests,
  IDLE_ACTIVITY_THROTTLE_MS,
} from '@/utils/idleTimer';

class MockBroadcastChannel {
  static instances: MockBroadcastChannel[] = [];
  onmessage: ((event: MessageEvent<{ type?: string; deadline?: number }>) => void) | null = null;
  postMessage = jest.fn((message: { type?: string; deadline?: number }) => {
    // Deliver to every other open channel of the same name, like the real API.
    for (const other of MockBroadcastChannel.instances) {
      if (other !== this && other.name === this.name && other.onmessage) {
        other.onmessage({ data: message } as MessageEvent<{ type?: string; deadline?: number }>);
      }
    }
  });
  close = jest.fn(() => {
    MockBroadcastChannel.instances = MockBroadcastChannel.instances.filter((c) => c !== this);
  });

  constructor(public name: string) {
    MockBroadcastChannel.instances.push(this);
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
    expect(onExpiredB).toHaveBeenCalledTimes(1);
    tabA.stop();
    tabB.stop();
  });
});
