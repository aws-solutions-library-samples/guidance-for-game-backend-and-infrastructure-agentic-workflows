/**
 * Theme bridge between the saved tri-state preference and Cloudscape.
 *
 * The application keeps one theme preference (`light`, `dark`, or `system`,
 * default `system`) resolved by `ThemeProvider`. This bridge drives Cloudscape's
 * visual mode from the already-resolved theme rather than the raw stored value,
 * so `system` and OS theme changes are honored.
 *
 * The `--ga-*` custom properties the shared component CSS reads are redefined in
 * the Cloudscape shell in terms of `@cloudscape-design/design-tokens` values. The
 * token package exposes hashed custom-property names (for example
 * `var(--color-text-body-default-b2us04, #0f141a)`), so the values are read from
 * the package at runtime and written with `style.setProperty` rather than
 * hard-coded in static CSS.
 */

// Third-party packages
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import * as tokens from '@cloudscape-design/design-tokens';

type ResolvedTheme = 'light' | 'dark';

/**
 * Map the `--ga-*` custom properties the shared component CSS consumes onto
 * Cloudscape design-token values. Only the tokens the shell actually needs are
 * mapped; both themes read the same token names because Cloudscape swaps the
 * underlying values when the visual mode changes.
 */
const GA_TOKEN_MAP: Readonly<Record<string, string>> = {
  '--ga-bg': tokens.colorBackgroundLayoutMain,
  '--ga-text': tokens.colorTextBodyDefault,
  '--ga-text-muted': tokens.colorTextBodySecondary,
  '--ga-text-subtle': tokens.colorTextBodySecondary,
  '--ga-accent': tokens.colorTextInteractiveDefault,
  '--ga-border': tokens.colorBorderDividerDefault,
  '--ga-surface': tokens.colorBackgroundContainerContent,
  '--ga-surface-strong': tokens.colorBackgroundContainerContent,
  '--ga-danger': tokens.colorTextStatusError,
  '--ga-success': tokens.colorTextStatusSuccess,
  '--ga-warning': tokens.colorTextStatusWarning,
};

/** Apply Cloudscape's visual mode for a resolved theme. */
export function applyCloudscapeMode(resolvedTheme: ResolvedTheme): void {
  if (typeof document === 'undefined') {
    return;
  }

  applyMode(resolvedTheme === 'light' ? Mode.Light : Mode.Dark, document.documentElement);
}

/**
 * Write the `--ga-*` token values onto a shell root element so first-party
 * component CSS resolves to Cloudscape colors in the new shell.
 */
export function applyGaTokenBridge(root: HTMLElement | null | undefined): void {
  if (!root) {
    return;
  }

  for (const [name, value] of Object.entries(GA_TOKEN_MAP)) {
    root.style.setProperty(name, value);
  }
}
