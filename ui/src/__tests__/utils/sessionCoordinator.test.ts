/**
 * Application-level session coordinator (#310, Blocker 3).
 *
 * Every logout entry (idle expiry/sign-out, protected-request 401 expiration,
 * refresh failure, cross-tab logout, header manual sign-out) MUST route through
 * one coordinator so that:
 *   1. A logout waits for (or invalidates) any in-flight refresh.
 *   2. The cookie-clearing logout is always the LAST auth-affecting response,
 *      so a late refresh success can never write auth cookies after logout.
 *   3. A later successful reauthentication works after the coordinator settles.
 */

import { createSessionCoordinator } from '@/utils/sessionCoordinator';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('createSessionCoordinator', () => {
  it('runs a refresh and reports success without logging out', async () => {
    const order: string[] = [];
    const coordinator = createSessionCoordinator({
      refresh: async () => {
        order.push('refresh');
        return true;
      },
      logout: async () => {
        order.push('logout');
      },
    });

    await expect(coordinator.refresh()).resolves.toBe(true);
    expect(order).toEqual(['refresh']);
  });

  it('a refresh failure triggers logout after the refresh settles', async () => {
    const order: string[] = [];
    const coordinator = createSessionCoordinator({
      refresh: async () => {
        order.push('refresh');
        return false;
      },
      logout: async () => {
        order.push('logout');
      },
    });

    const ok = await coordinator.refresh();
    expect(ok).toBe(false);
    // The failed refresh must drive a logout, cookie clearing last.
    await coordinator.settled();
    expect(order).toEqual(['refresh', 'logout']);
  });

  it('logout defers until an in-flight refresh settles, and clears cookies LAST', async () => {
    const gate = deferred<boolean>();
    const order: string[] = [];
    const coordinator = createSessionCoordinator({
      refresh: () => {
        order.push('refresh-start');
        return gate.promise;
      },
      logout: async () => {
        order.push('logout');
      },
    });

    // Start a refresh (pending) — e.g. "Stay signed in".
    const refreshPromise = coordinator.refresh();

    // A logout entry (idle expiry / cross-tab / manual sign-out) fires while the
    // refresh is still in flight.
    const logoutPromise = coordinator.logout();

    // Logout must NOT have cleared cookies yet — the refresh is still pending.
    await Promise.resolve();
    expect(order).toEqual(['refresh-start']);

    // The refresh finally resolves successfully. Its response is discarded and
    // the deferred logout runs last.
    gate.resolve(true);
    await refreshPromise;
    await logoutPromise;

    expect(order).toEqual(['refresh-start', 'logout']);
    expect(coordinator.isLoggedOut()).toBe(true);
  });

  it('a refresh that settles after logout does not resurrect the session', async () => {
    const gate = deferred<boolean>();
    const coordinator = createSessionCoordinator({
      refresh: () => gate.promise,
      logout: async () => undefined,
    });

    const refreshPromise = coordinator.refresh();
    const logoutPromise = coordinator.logout();
    gate.resolve(true); // late success
    await refreshPromise;
    await logoutPromise;

    // The coordinator remains terminally logged out; the late success is stale.
    expect(coordinator.isLoggedOut()).toBe(true);
  });

  it('coalesces multiple concurrent logout entries into a single cookie clear', async () => {
    let logoutCalls = 0;
    const coordinator = createSessionCoordinator({
      refresh: async () => true,
      logout: async () => {
        logoutCalls += 1;
      },
    });

    await Promise.all([coordinator.logout(), coordinator.logout(), coordinator.logout()]);
    expect(logoutCalls).toBe(1);
    expect(coordinator.isLoggedOut()).toBe(true);
  });

  it('supports a fresh authenticated session after a prior logout (reauthentication)', async () => {
    let logoutCalls = 0;
    const coordinator = createSessionCoordinator({
      refresh: async () => true,
      logout: async () => {
        logoutCalls += 1;
      },
    });

    await coordinator.logout();
    expect(coordinator.isLoggedOut()).toBe(true);
    expect(logoutCalls).toBe(1);

    // A new authenticated session resets the terminal state so refresh works
    // again for the newly signed-in user.
    coordinator.beginSession();
    expect(coordinator.isLoggedOut()).toBe(false);
    await expect(coordinator.refresh()).resolves.toBe(true);
  });

  it('a logout entry after a refresh rejects still clears cookies last', async () => {
    const gate = deferred<boolean>();
    const order: string[] = [];
    const coordinator = createSessionCoordinator({
      refresh: () => {
        order.push('refresh-start');
        return gate.promise;
      },
      logout: async () => {
        order.push('logout');
      },
    });

    const refreshPromise = coordinator.refresh();
    const logoutPromise = coordinator.logout();
    gate.reject(new Error('network')); // refresh fails after logout requested
    await refreshPromise.catch(() => undefined);
    await logoutPromise;

    expect(order).toEqual(['refresh-start', 'logout']);
    expect(coordinator.isLoggedOut()).toBe(true);
  });
});
