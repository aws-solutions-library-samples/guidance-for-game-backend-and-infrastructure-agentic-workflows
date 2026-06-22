/**
 * fetch() with a client-side timeout.
 *
 * Plain fetch() has no default timeout, so a slow/hung backend leaves the UI
 * spinning indefinitely (#130). This wraps fetch with an AbortController that
 * aborts after `timeoutMs`, surfacing a clear error the caller can handle.
 *
 * Uses AbortController + setTimeout (universally supported) rather than
 * AbortSignal.timeout (newer lib types) for broad compatibility.
 */
export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = 15000
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
