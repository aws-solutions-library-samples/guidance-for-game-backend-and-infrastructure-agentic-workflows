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
 * other pages are untouched. It never sets, removes, or weakens a response
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

export function proxy(request: NextRequest) {
  const shell = resolveShellSelection(process.env[SHELL_ENV_VAR]);

  if (request.nextUrl.pathname === CLOUDSCAPE_PAGE) {
    // When the Cloudscape shell is selected, `/` is rewritten here internally
    // and the proxy re-runs on the rewritten path; let that through. When it is
    // not selected, a request reaching this route came from an external caller
    // addressing it directly, so return 404 to keep the internal route hidden.
    return shell === 'cloudscape' ? NextResponse.next() : new NextResponse(null, { status: 404 });
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
