/**
 * The server proxy selects the shell for `/` and guards the internal Cloudscape
 * route. It MUST rewrite `/` to the Cloudscape page only when `GBAW_UI_SHELL` is
 * exactly `cloudscape`, serve the current UI otherwise, and serve the app's own
 * 404 page with a 404 status for a direct request to `/shell-cloudscape` UNLESS
 * the Cloudscape shell is selected (in which case `/` is rewritten there
 * internally and the proxy re-runs on it, so that re-entry must be allowed
 * through).
 *
 * `next/server` is mocked so the decision is observable without the Next
 * runtime; the end-to-end rewrite/404 behavior is verified against a real build
 * separately.
 */
jest.mock('next/server', () => {
  const mockRewrite = jest.fn((url: unknown, init: { status?: number } = {}) => ({
    kind: 'rewrite',
    url,
    status: init.status,
  }));
  const mockNext = jest.fn(() => ({ kind: 'next' }));
  return {
    NextResponse: Object.assign(
      // Constructor form: new NextResponse(body, init) → captured as a response.
      jest.fn((_body: unknown, init: { status?: number } = {}) => ({
        kind: 'response',
        status: init.status,
      })),
      { rewrite: mockRewrite, next: mockNext },
    ),
  };
});

import { config, proxy } from '@/proxy';
import { CLOUDSCAPE_PAGE } from '@/shell/shell';

type Decision = { kind: string; status?: number; url?: { pathname: string } };

function requestFor(pathname: string): import('next/server').NextRequest {
  const nextUrl = {
    pathname,
    clone() {
      return { pathname: this.pathname };
    },
  };
  return { nextUrl } as unknown as import('next/server').NextRequest;
}

describe('proxy shell selection', () => {
  const original = process.env.GBAW_UI_SHELL;

  afterEach(() => {
    jest.clearAllMocks();
    if (original === undefined) {
      delete process.env.GBAW_UI_SHELL;
    } else {
      process.env.GBAW_UI_SHELL = original;
    }
  });

  it('serves the current UI for `/` when the selector is unset', () => {
    delete process.env.GBAW_UI_SHELL;
    const result = proxy(requestFor('/')) as unknown as Decision;
    expect(result.kind).toBe('next');
  });

  it('rewrites `/` to the Cloudscape page when the selector is cloudscape', () => {
    process.env.GBAW_UI_SHELL = 'cloudscape';
    const result = proxy(requestFor('/')) as unknown as Decision;
    expect(result.kind).toBe('rewrite');
    expect(result.url?.pathname).toBe(CLOUDSCAPE_PAGE);
  });

  it('serves the app 404 page for a direct /shell-cloudscape request when the selector is unset', () => {
    delete process.env.GBAW_UI_SHELL;
    const result = proxy(requestFor(CLOUDSCAPE_PAGE)) as unknown as Decision;
    // The hidden route is rewritten to the app's own 404 page, keeping a 404
    // status rather than returning a bare empty-body response.
    expect(result.kind).toBe('rewrite');
    expect(result.url?.pathname).toBe('/404');
    expect(result.status).toBe(404);
  });

  it('serves the app 404 page for a direct /shell-cloudscape request for a non-cloudscape value', () => {
    process.env.GBAW_UI_SHELL = 'Cloudscape'; // wrong case → current shell
    const result = proxy(requestFor(CLOUDSCAPE_PAGE)) as unknown as Decision;
    expect(result.kind).toBe('rewrite');
    expect(result.url?.pathname).toBe('/404');
    expect(result.status).toBe(404);
  });

  it('allows the internal /shell-cloudscape re-entry when the shell is selected', () => {
    process.env.GBAW_UI_SHELL = 'cloudscape';
    const result = proxy(requestFor(CLOUDSCAPE_PAGE)) as unknown as Decision;
    // The `/` rewrite re-runs the proxy on this path; it must pass through, not 404.
    expect(result.kind).toBe('next');
  });
});

/**
 * ADR 0007's authentication boundary: the proxy must never run on, or rewrite,
 * the chat proxy or the auth routes. The matcher keeps it off every other path,
 * and the proxy itself passes through any path other than `/` and the hidden
 * route, so a widened matcher still could not rewrite an API route.
 */
describe('proxy authentication boundary', () => {
  const BOUNDARY_PATHS = [
    '/api/copilot/chat',
    '/api/auth/login',
    '/api/auth/logout',
    '/api/auth/refresh',
    '/api/auth/user',
    '/api/config',
    '/api/health',
  ];
  const original = process.env.GBAW_UI_SHELL;

  afterEach(() => {
    jest.clearAllMocks();
    if (original === undefined) {
      delete process.env.GBAW_UI_SHELL;
    } else {
      process.env.GBAW_UI_SHELL = original;
    }
  });

  it('matches exactly the home page and the hidden Cloudscape route', () => {
    expect(config.matcher).toEqual(['/', CLOUDSCAPE_PAGE]);
  });

  it('uses only literal matcher paths, so no API route can match', () => {
    // A literal Next matcher path matches only itself; a parameter, wildcard,
    // or group would widen it beyond the two routes the proxy owns.
    for (const entry of config.matcher) {
      expect(entry).toMatch(/^\/[a-z-]*$/);
    }
    for (const path of BOUNDARY_PATHS) {
      expect(config.matcher).not.toContain(path);
    }
  });

  it.each(BOUNDARY_PATHS)('passes %s through untouched with the Cloudscape shell selected', (path) => {
    process.env.GBAW_UI_SHELL = 'cloudscape';
    const result = proxy(requestFor(path)) as unknown as Decision;
    expect(result.kind).toBe('next');
  });

  it.each(BOUNDARY_PATHS)('passes %s through untouched with the selector unset', (path) => {
    delete process.env.GBAW_UI_SHELL;
    const result = proxy(requestFor(path)) as unknown as Decision;
    expect(result.kind).toBe('next');
  });
});
