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

// Mock the full-page reload so the hook's boundary decision is observable
// without a real navigation (jsdom does not implement reload). The mock
// delegates to the real implementation so the one-shot `sessionStorage` guard
// still runs; `reloadMock` therefore counts how often the hook *asked* to
// reload, while `windowReloadSpy` counts the full loads that actually happened
// (bounded to one per URL). The real shell-selection logic (documentShell,
// clearShellBoundaryReloadMarker) is kept.
jest.mock('@/shell/shell', () => {
  const actual = jest.requireActual('@/shell/shell');
  return {
    ...actual,
    reloadForShellBoundary: jest.fn((...args: unknown[]) => actual.reloadForShellBoundary(...args)),
  };
});

jest.mock('next/router', () => ({
  useRouter: () => ({ route: '/', pathname: '/', query: {}, asPath: '/' }),
}));

const reloadMock = reloadForShellBoundary as jest.Mock;

type ShellPage = (() => React.ReactElement) & { shell?: Shell };

const CurrentPage: ShellPage = () => <div data-testid="app-content">current</div>;

const CloudscapePage: ShellPage = () => <div data-testid="app-content">cloudscape</div>;
CloudscapePage.shell = 'cloudscape';

// Stand-in for Next's built-in error page: production `renderError` re-renders
// `_app` with this shell-less component and an `err` prop. It must never be
// treated as a shell crossing.
const ErrorPage: ShellPage = () => <div data-testid="app-content">error</div>;

function setDocumentShell(shell: Shell | undefined): void {
  if (shell === undefined) {
    delete document.documentElement.dataset.gaShell;
  } else {
    document.documentElement.dataset.gaShell = shell;
  }
}

describe('MyApp cross-shell boundary reload', () => {
  // Count the full loads the one-shot guard actually issued (mock results that
  // returned `true`), independent of how often the hook asked.
  function actualReloads(): number {
    return reloadMock.mock.results.filter((r) => r.type === 'return' && r.value === true).length;
  }

  beforeEach(() => {
    jest.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
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

  it('reloads at most once when the same URL keeps rendering a mismatched shell', async () => {
    // Next's production `renderError` re-renders `_app` with the shell-less
    // error page for the same URL on every client exception. Without a bound
    // this reloads forever; the one-shot `sessionStorage` guard must cap the
    // actual full loads at one per URL.
    setDocumentShell('cloudscape');
    const first = render(<MyApp Component={CurrentPage} pageProps={{}} />);
    await waitFor(() => expect(actualReloads()).toBe(1));
    first.unmount();

    // A second render for the same URL (jsdom keeps the loader's href fixed)
    // must not reload again, even though the hook still sees a shell mismatch.
    render(<MyApp Component={CurrentPage} pageProps={{}} />);
    await waitFor(() => expect(document.querySelector('[data-testid="app-content"]')).toBeTruthy());
    expect(reloadMock.mock.calls.length).toBeGreaterThan(1);
    expect(actualReloads()).toBe(1);
  });

  it('does not reload for an error render even when the error page has no shell', async () => {
    // Production `renderError` passes `err` and the shell-less error page into
    // `_app`. Inside a Cloudscape document that is a shell mismatch, but it must
    // never reload: reloading would re-run the same failing render.
    setDocumentShell('cloudscape');
    render(<MyApp Component={ErrorPage} pageProps={{}} err={new Error('client-side exception')} />);
    await waitFor(() => expect(document.querySelector('[data-testid="app-content"]')).toBeTruthy());
    expect(reloadMock).not.toHaveBeenCalled();
    expect(actualReloads()).toBe(0);
  });
});
