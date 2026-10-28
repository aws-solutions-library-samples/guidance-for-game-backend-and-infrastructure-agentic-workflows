/**
 * Shell selection shared across the server proxy, `_document`, and `_app`.
 *
 * The application ships two UI shells behind one set of URLs. The current shell
 * is the default and is served at `/` unless the server-only `GBAW_UI_SHELL`
 * value selects the Cloudscape shell. The Cloudscape shell lives on its own
 * internal page; a direct request to it returns the app's 404 page unless the
 * Cloudscape shell is selected, it does no gating of its own, and it is
 * presentation-only. It is served at `/` only when selected; see `proxy.ts`.
 */

export type Shell = 'current' | 'cloudscape';

export const DEFAULT_SHELL: Shell = 'current';

/** Server-only environment variable that selects the default shell for `/`. */
export const SHELL_ENV_VAR = 'GBAW_UI_SHELL';

/** The internal page that renders the Cloudscape shell. */
export const CLOUDSCAPE_PAGE = '/shell-cloudscape';

/**
 * The `__NEXT_DATA__.page` values that render in the Cloudscape shell. Any page
 * not listed here renders in the current shell, so adding a page is opt-in.
 */
const SHELL_BY_PAGE: Record<string, Shell> = {
  [CLOUDSCAPE_PAGE]: 'cloudscape',
};

/** Resolve the shell for a Next page route, defaulting to the current shell. */
export function shellForPage(page: string | undefined | null): Shell {
  if (!page) {
    return DEFAULT_SHELL;
  }

  return SHELL_BY_PAGE[page] ?? DEFAULT_SHELL;
}

/**
 * Resolve the selector value from its raw environment string. Only the exact
 * value `cloudscape` selects the Cloudscape shell; everything else (including an
 * unset value or any typo) falls back to the current shell, so the new shell is
 * never served by accident.
 */
export function resolveShellSelection(rawValue: string | undefined | null): Shell {
  return rawValue === 'cloudscape' ? 'cloudscape' : DEFAULT_SHELL;
}

/**
 * The shell the current document was rendered for, read from the `data-ga-shell`
 * attribute `_document` wrote into the server HTML, defaulting to the current
 * shell when the attribute is absent.
 */
export function documentShell(): Shell {
  if (typeof document === 'undefined') {
    return DEFAULT_SHELL;
  }
  return (document.documentElement.dataset.gaShell as Shell | undefined) ?? DEFAULT_SHELL;
}

/**
 * `sessionStorage` key holding the URL a shell-boundary reload was last issued
 * for. The reload is one-shot per URL: a URL that has already triggered a reload
 * is not reloaded again, so a render that keeps producing a shell mismatch for
 * the same URL (for example the client error page, which carries no shell inside
 * a Cloudscape document) cannot loop. The marker is scoped to the tab and
 * cleared the moment a render's shell matches the document, so a later genuine
 * boundary crossing to the same URL still reloads.
 */
const SHELL_BOUNDARY_RELOAD_MARKER = 'ga-shell-boundary-reload';

function sessionStorageOrNull(): Storage | null {
  if (typeof window === 'undefined') {
    return null;
  }
  try {
    return window.sessionStorage;
  } catch {
    // Access to sessionStorage can throw (for example in a sandboxed document);
    // treat that as "no marker store" rather than failing the render.
    return null;
  }
}

/**
 * Force a full document load, at most once per URL. Used to cross the shell
 * boundary on a client-side navigation so the correct document is fetched and
 * the two design systems are never mixed. A reload adds no history entry, so
 * browser Back still works.
 *
 * Before reloading, the current URL is written to a `sessionStorage` marker; if
 * the marker already holds the current URL the reload is skipped. This bounds
 * the reload to one per URL so a persistently mismatched render (such as the
 * client error page) cannot reload forever. Returns `true` when a reload was
 * issued and `false` when it was skipped by the one-shot guard or no window is
 * available.
 */
export function reloadForShellBoundary(): boolean {
  if (typeof window === 'undefined') {
    return false;
  }
  const store = sessionStorageOrNull();
  const currentUrl = window.location.href;
  if (store) {
    if (store.getItem(SHELL_BOUNDARY_RELOAD_MARKER) === currentUrl) {
      return false;
    }
    try {
      store.setItem(SHELL_BOUNDARY_RELOAD_MARKER, currentUrl);
    } catch {
      // If the marker cannot be persisted the reload still runs; the guard only
      // adds protection when it can, and never blocks a needed reload.
    }
  }
  window.location.reload();
  return true;
}

/**
 * Clear the one-shot shell-boundary reload marker. Called on any render whose
 * shell already matches the document, so the correct-document render always
 * re-arms the guard for a future boundary crossing to the same URL.
 */
export function clearShellBoundaryReloadMarker(): void {
  const store = sessionStorageOrNull();
  if (!store) {
    return;
  }
  try {
    store.removeItem(SHELL_BOUNDARY_RELOAD_MARKER);
  } catch {
    // A failure to clear the marker is harmless: at worst a future reload for
    // the same URL is skipped, which only reduces reloads, never causes a loop.
  }
}
