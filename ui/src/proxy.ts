/**
 * Server-side shell selection for `/`.
 *
 * The frontend chooses which UI shell renders `/` at request time from the
 * server-only `GBAW_UI_SHELL` value. The current shell is the default and the
 * only one served unless `GBAW_UI_SHELL` is exactly `cloudscape`, in which case
 * `/` is internally rewritten to the Cloudscape page and served at the same URL
 * with no redirect. The value is read per request from a non-public environment
 * variable, so it is never inlined into client bundles and can be flipped by
 * configuration without a rebuild.
 *
 * This proxy rewrites `/` and guards the internal Cloudscape route. The matcher
 * covers only `/` and `/shell-cloudscape`, so API routes, static assets, and all
 * other pages are untouched, and the proxy itself passes any other path through
 * unchanged. It never sets, removes, or weakens a response
 * header or the Content Security Policy, and it adds no authentication: the
 * rewritten page runs through exactly the same session and sign-in flow as the
 * current shell. When the Cloudscape shell is not selected, a direct request to
 * the internal `/shell-cloudscape` route returns 404 so the route is reachable
 * only through the `/` rewrite; when it is selected, `/` is rewritten to that
 * route and the proxy re-runs on it, so that internal re-entry is allowed
 * through.
 */

// Third-party packages
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// Local modules
import { CLOUDSCAPE_PAGE, resolveShellSelection, SHELL_ENV_VAR } from '@/shell/shell';

/** The app's own not-found page, served for a direct hit on the hidden route. */
const NOT_FOUND_PAGE = '/404';

export function proxy(request: NextRequest) {
  const shell = resolveShellSelection(process.env[SHELL_ENV_VAR]);

  if (request.nextUrl.pathname === CLOUDSCAPE_PAGE) {
    // When the Cloudscape shell is selected, `/` is rewritten here internally
    // and the proxy re-runs on the rewritten path; let that through. When it is
    // not selected, a request reaching this route came from an external caller
    // addressing it directly, so rewrite to the app's own 404 page with a 404
    // status to keep the internal route hidden while serving the same
    // not-found experience (and security headers) as any other unknown path.
    if (shell === 'cloudscape') {
      return NextResponse.next();
    }
    const notFoundUrl = request.nextUrl.clone();
    notFoundUrl.pathname = NOT_FOUND_PAGE;
    return NextResponse.rewrite(notFoundUrl, { status: 404 });
  }

  // Only `/` is ever rewritten. The matcher already keeps the proxy off every
  // other path; this check keeps a widened matcher from rewriting an API route
  // such as `/api/copilot/chat` or `/api/auth/*`.
  if (request.nextUrl.pathname !== '/') {
    return NextResponse.next();
  }

  if (shell === 'cloudscape') {
    const url = request.nextUrl.clone();
    url.pathname = CLOUDSCAPE_PAGE;
    // Rewrite (not redirect): the response is served at `/`, the URL the user
    // sees never changes, and `/` stays statically prerendered.
    return NextResponse.rewrite(url);
  }

  return NextResponse.next();
}

export const config = {
  // Match the home page and the internal Cloudscape route. Everything else —
  // API routes, static assets, and every other page — bypasses this proxy.
  matcher: ['/', '/shell-cloudscape'],
};
