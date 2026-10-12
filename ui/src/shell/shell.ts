/**
 * Shell selection shared across the server proxy, `_document`, and `_app`.
 *
 * The application ships two UI shells behind one set of URLs. The current shell
 * is the default and the only one reachable without an explicit opt-in. The
 * Cloudscape shell lives on its own internal page and is served at `/` only when
 * the server-only `GBAW_UI_SHELL` value selects it; see `proxy.ts`.
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
