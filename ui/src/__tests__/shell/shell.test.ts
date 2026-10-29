/**
 * Shell selection is the switch that keeps the Cloudscape shell dormant. The
 * current shell MUST be the default, and ONLY the exact value `cloudscape` may
 * select the new shell — any other value (unset, empty, a typo, or a truthy
 * string) must fall back to the current shell so the new shell is never served
 * by accident.
 */

import {
  CLOUDSCAPE_PAGE,
  DEFAULT_SHELL,
  clearShellBoundaryReloadMarker,
  documentShell,
  reloadForShellBoundary,
  resolveShellSelection,
  shellForPage,
} from '@/shell/shell';

describe('resolveShellSelection', () => {
  it('selects the Cloudscape shell only for the exact value "cloudscape"', () => {
    expect(resolveShellSelection('cloudscape')).toBe('cloudscape');
  });

  it.each([undefined, null, '', 'current', 'Cloudscape', 'CLOUDSCAPE', 'true', 'cloudscape '])(
    'falls back to the current shell for %p',
    (value) => {
      expect(resolveShellSelection(value as string | undefined | null)).toBe('current');
    },
  );

  it('defaults to the current shell', () => {
    expect(DEFAULT_SHELL).toBe('current');
  });
});

describe('shellForPage', () => {
  it('maps the Cloudscape entry page to the Cloudscape shell', () => {
    expect(shellForPage(CLOUDSCAPE_PAGE)).toBe('cloudscape');
  });

  it.each(['/', '/admin/users', '/404', undefined, null, '/unknown'])(
    'maps %p to the current shell',
    (page) => {
      expect(shellForPage(page as string | undefined | null)).toBe('current');
    },
  );
});

describe('documentShell', () => {
  afterEach(() => {
    delete document.documentElement.dataset.gaShell;
  });

  it('reads the Cloudscape shell from the data-ga-shell attribute', () => {
    document.documentElement.dataset.gaShell = 'cloudscape';
    expect(documentShell()).toBe('cloudscape');
  });

  it('reads the current shell from the data-ga-shell attribute', () => {
    document.documentElement.dataset.gaShell = 'current';
    expect(documentShell()).toBe('current');
  });

  it('defaults to the current shell when the attribute is absent', () => {
    expect(documentShell()).toBe('current');
  });
});

describe('reloadForShellBoundary one-shot guard', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
  });

  it('issues a reload the first time it is called for a URL', () => {
    expect(reloadForShellBoundary()).toBe(true);
  });

  it('skips a second reload for the same URL (so a mismatch cannot loop)', () => {
    expect(reloadForShellBoundary()).toBe(true);
    expect(reloadForShellBoundary()).toBe(false);
    expect(reloadForShellBoundary()).toBe(false);
  });

  it('re-arms after the marker is cleared, so a later crossing reloads again', () => {
    expect(reloadForShellBoundary()).toBe(true);
    expect(reloadForShellBoundary()).toBe(false);
    clearShellBoundaryReloadMarker();
    expect(reloadForShellBoundary()).toBe(true);
  });
});
