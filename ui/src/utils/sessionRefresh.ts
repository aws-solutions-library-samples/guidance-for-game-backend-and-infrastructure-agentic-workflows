const CHANNEL_NAME = 'game-agent-session';
const REFRESH_LOCK_NAME = 'game-agent-session-refresh';
const LAST_REFRESH_KEY = 'game-agent-last-session-refresh';
const RECENT_REFRESH_MS = 5_000;
const REFRESH_TIMEOUT_MS = 10_000;

let inFlightRefresh: Promise<boolean> | null = null;

type FetchLike = typeof window.fetch;

function broadcast(type: 'refreshed' | 'expired'): void {
  if (typeof BroadcastChannel === 'undefined') return;
  const channel = new BroadcastChannel(CHANNEL_NAME);
  channel.postMessage({ type });
  channel.close();
}

function wasRecentlyRefreshed(now: number): boolean {
  try {
    const value = Number(window.localStorage.getItem(LAST_REFRESH_KEY));
    return Number.isFinite(value) && now - value < RECENT_REFRESH_MS;
  } catch {
    return false;
  }
}

function recordRefresh(now: number): void {
  try {
    window.localStorage.setItem(LAST_REFRESH_KEY, String(now));
  } catch {
    // Refresh still succeeds when storage is disabled.
  }
}

async function performRefresh(fetchImpl: FetchLike): Promise<boolean> {
  const now = Date.now();
  if (wasRecentlyRefreshed(now)) return true;

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REFRESH_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetchImpl('/api/auth/refresh', { method: 'POST', signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
  if (!response.ok) {
    broadcast('expired');
    return false;
  }
  recordRefresh(Date.now());
  broadcast('refreshed');
  return true;
}

async function refreshWithCrossTabLock(fetchImpl: FetchLike): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.locks) {
    return navigator.locks.request(REFRESH_LOCK_NAME, () => performRefresh(fetchImpl));
  }
  return performRefresh(fetchImpl);
}

export function refreshSessionOnce(fetchImpl: FetchLike): Promise<boolean> {
  if (!inFlightRefresh) {
    inFlightRefresh = refreshWithCrossTabLock(fetchImpl)
      .catch(() => false)
      .finally(() => {
        inFlightRefresh = null;
      });
  }
  return inFlightRefresh;
}

export function subscribeToSessionExpiration(onExpired: () => void): () => void {
  if (typeof BroadcastChannel === 'undefined') return () => undefined;
  const channel = new BroadcastChannel(CHANNEL_NAME);
  channel.onmessage = (event: MessageEvent<{ type?: string }>) => {
    if (event.data?.type === 'expired') onExpired();
  };
  return () => channel.close();
}

export function clearSessionRefreshMarker(): void {
  try {
    window.localStorage.removeItem(LAST_REFRESH_KEY);
  } catch {
    // Session state remains secure when storage is disabled.
  }
}

export function resetRefreshCoordinatorForTests(): void {
  inFlightRefresh = null;
  clearSessionRefreshMarker();
}
