/**
 * Internal entry page for the Cloudscape shell.
 *
 * The server proxy rewrites `/` to this route when `GBAW_UI_SHELL=cloudscape`;
 * no deployment default selects it and nothing links to it. The route is
 * directly addressable, but it is authentication-gated and presentation-only:
 * it reuses the same session and sign-in flow as the current shell and adds no
 * API route or data of its own. The heavy Cloudscape layout and
 * `@cloudscape-design/global-styles` load only through this entry (via
 * `next/dynamic`), so no Cloudscape code enters `_app` or any current-UI page.
 *
 * Authentication is reused unchanged: `_app` wraps this page in the same session
 * and sign-in flow as the current shell, so an unauthenticated visitor sees the
 * same sign-in behavior. This page adds no auth code and no API route.
 */

// Third-party packages
import type { ReactElement } from 'react';
import Head from 'next/head';
import dynamic from 'next/dynamic';

// Local modules
import type { Shell } from '@/shell/shell';

const CloudscapeShell = dynamic(() => import('../components/cloudscape/CloudscapeShell'), {
  ssr: true,
});

type ShellCloudscapePage = (() => ReactElement) & { shell: Shell };

const ShellCloudscape: ShellCloudscapePage = () => (
  <>
    <Head>
      <title>Game Agent - AI-Powered Game Server Management</title>
    </Head>
    <CloudscapeShell />
  </>
);

// Read by `_app` to select the per-shell boundary reload and by `_document`
// through the page route; marks this page as the Cloudscape shell.
ShellCloudscape.shell = 'cloudscape';

export default ShellCloudscape;
