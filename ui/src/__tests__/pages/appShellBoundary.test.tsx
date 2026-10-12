/**
 * `_app` must keep the two shells from mixing in one document across a
 * client-side navigation. `data-ga-shell` is written once into the server HTML
 * and never updated by the client router, and the server proxy answers the
 * router's data request for `/`, so a soft navigation can leave the active
 * page's shell different from the document's shell. When that happens, `_app`
 * MUST force a full document load so the correct document (and only one design
 * system) is served; when they match, it MUST NOT reload.
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import MyApp from '../../pages/_app';
import type { Shell } from '@/shell/shell';
import { reloadForShellBoundary } from '@/shell/shell';

// Mock only the full-page reload so the boundary decision is observable without
// a real navigation (jsdom does not implement reload). The real shell-selection
// logic (documentShell, resolveShellSelection) is kept.
jest.mock('@/shell/shell', () => {
  const actual = jest.requireActual('@/shell/shell');
  return { ...actual, reloadForShellBoundary: jest.fn() };
});

jest.mock('next/router', () => ({
  useRouter: () => ({ route: '/', pathname: '/', query: {}, asPath: '/' }),
}));

const reloadMock = reloadForShellBoundary as jest.Mock;

type ShellPage = (() => React.ReactElement) & { shell?: Shell };

const CurrentPage: ShellPage = () => <div data-testid="app-content">current</div>;

const CloudscapePage: ShellPage = () => <div data-testid="app-content">cloudscape</div>;
CloudscapePage.shell = 'cloudscape';

function setDocumentShell(shell: Shell | undefined): void {
  if (shell === undefined) {
    delete document.documentElement.dataset.gaShell;
  } else {
    document.documentElement.dataset.gaShell = shell;
  }
}

describe('MyApp cross-shell boundary reload', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    window.localStorage.clear();
    // Keep auth out of the way: dev bypass renders the page directly.
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    process.env.NODE_ENV = 'development';
    global.fetch = jest.fn().mockResolvedValue({
      json: async () => ({ cognito: { region: 'us-west-2', userPoolId: 'p', clientId: 'c' } }),
    }) as unknown as typeof global.fetch;
  });

  afterEach(() => {
    setDocumentShell(undefined);
  });

  it('forces a full load when a current-shell document renders a Cloudscape page', async () => {
    setDocumentShell('current');
    render(<MyApp Component={CloudscapePage} pageProps={{}} />);
    await waitFor(() => expect(reloadMock).toHaveBeenCalledTimes(1));
  });

  it('forces a full load when a Cloudscape document renders a current-shell page', async () => {
    setDocumentShell('cloudscape');
    render(<MyApp Component={CurrentPage} pageProps={{}} />);
    await waitFor(() => expect(reloadMock).toHaveBeenCalledTimes(1));
  });

  it('does not reload when the page shell matches the document shell (current)', async () => {
    setDocumentShell('current');
    render(<MyApp Component={CurrentPage} pageProps={{}} />);
    await waitFor(() => expect(document.querySelector('[data-testid="app-content"]')).toBeTruthy());
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it('does not reload when the page shell matches the document shell (cloudscape)', async () => {
    setDocumentShell('cloudscape');
    render(<MyApp Component={CloudscapePage} pageProps={{}} />);
    await waitFor(() => expect(document.querySelector('[data-testid="app-content"]')).toBeTruthy());
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it('treats a missing document shell attribute as the current shell', async () => {
    setDocumentShell(undefined);
    render(<MyApp Component={CurrentPage} pageProps={{}} />);
    await waitFor(() => expect(document.querySelector('[data-testid="app-content"]')).toBeTruthy());
    expect(reloadMock).not.toHaveBeenCalled();
  });
});
