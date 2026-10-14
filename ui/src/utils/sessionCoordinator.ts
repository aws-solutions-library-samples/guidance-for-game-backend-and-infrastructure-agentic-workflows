/**
 * Application-level session coordinator (#310, Blocker 3).
 *
 * The idle hook previously serialized refresh-vs-logout only within its own
 * closure, so other logout entries — protected-request 401 expiration
 * (`_app`), refresh-failure broadcasts, cross-tab logout, and the header manual
 * sign-out — could clear cookies while an idle refresh was still in flight. A
 * late `/api/auth/refresh` success could then write auth cookies AFTER the
 * logout cleared them.
 *
 * This coordinator is the single place every logout entry routes through. It
 * guarantees:
 *   1. A logout requested while a refresh is pending is deferred until that
 *      refresh settles ("logout waits for an in-flight refresh").
 *   2. Once logout is requested the session is terminally logged out, so a late
 *      refresh success is discarded ("logout wins" / "invalidate in-flight
 *      refresh").
 *   3. The cookie-clearing `logout` side effect is the LAST auth-affecting
 *      response.
 *   4. Multiple concurrent logout entries coalesce into one cookie clear.
 *   5. `beginSession()` resets the terminal state so a later successful
 *      reauthentication works.
 *
 * This remains a UX/local-exposure control. The server still verifies tokens on
 * every request and is the authorization boundary.
 */

export interface SessionCoordinatorOptions {
  /** Runs the secure refresh (#309). Resolves true on success. */
  refresh: () => Promise<boolean>;
  /** Clears server cookies + local state. Must be the last auth response. */
  logout: () => void | Promise<void>;
}

export interface SessionCoordinator {
  /** Run a refresh serialized against logout. Resolves the refresh result.
   * Resolves false if a logout latched terminal while the refresh was pending,
   * even if the raw refresh succeeded — so callers never act on a stale success.
   */
  refresh(): Promise<boolean>;
  /**
   * Request logout from any entry. Waits for an in-flight refresh to settle,
   * then clears cookies exactly once. Idempotent while terminal.
   */
  logout(): Promise<void>;
  /** True once a logout has been requested (terminal until beginSession). */
  isLoggedOut(): boolean;
  /** Resolves when all pending refresh/logout work has settled. */
  settled(): Promise<void>;
  /** Begin a fresh authenticated session, clearing terminal logout state. */
  beginSession(): void;
}

export function createSessionCoordinator(options: SessionCoordinatorOptions): SessionCoordinator {
  const { refresh: doRefresh, logout: doLogout } = options;

  let loggedOut = false;
  let pendingRefresh: Promise<boolean> | null = null;
  // A single logout promise; concurrent entries all await it so cookies clear
  // exactly once.
  let logoutPromise: Promise<void> | null = null;

  function runLogout(): Promise<void> {
    if (logoutPromise) return logoutPromise;
    loggedOut = true;
    if (!pendingRefresh) {
      // No refresh in flight: clear cookies immediately (and synchronously kick
      // off `doLogout`) so callers relying on prompt logout are not delayed by
      // an extra microtask hop.
      logoutPromise = Promise.resolve(doLogout());
      return logoutPromise;
    }
    // A refresh is in flight: defer the cookie clear until it settles so the
    // refresh response can never land after the logout clear. `pendingRefresh`
    // never rejects to the caller (refresh() swallows rejection), but guard
    // anyway so a rejection cannot skip the cookie clear.
    logoutPromise = pendingRefresh
      .then(() => undefined, () => undefined)
      .then(() => Promise.resolve(doLogout()));
    return logoutPromise;
  }

  return {
    refresh(): Promise<boolean> {
      if (loggedOut) return Promise.resolve(false);
      const promise = Promise.resolve()
        .then(() => doRefresh())
        .then(
          (ok) => {
            // A refresh failure is itself a logout entry.
            if (!ok && !loggedOut) {
              void runLogout();
            }
            // If a logout latched terminal WHILE this refresh was pending, the
            // raw success is stale: the session is already gone. Report the
            // terminal outcome (false) so a protected-request 401 caller does
            // not retry the protected request after logout, and so no caller
            // treats a discarded late success as a live session.
            if (loggedOut) return false;
            return ok;
          },
          () => {
            if (!loggedOut) void runLogout();
            return false;
          },
        )
        .finally(() => {
          if (pendingRefresh === promise) pendingRefresh = null;
        });
      pendingRefresh = promise;
      return promise;
    },

    logout(): Promise<void> {
      return runLogout();
    },

    isLoggedOut(): boolean {
      return loggedOut;
    },

    settled(): Promise<void> {
      return Promise.resolve()
        .then(() => pendingRefresh ?? undefined)
        .catch(() => undefined)
        .then(() => logoutPromise ?? undefined)
        .then(() => undefined);
    },

    beginSession(): void {
      loggedOut = false;
      pendingRefresh = null;
      logoutPromise = null;
    },
  };
}
