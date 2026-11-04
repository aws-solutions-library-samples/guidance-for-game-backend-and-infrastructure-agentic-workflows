import Document, { Html, Head, Main, NextScript } from 'next/document';
import type { DocumentProps } from 'next/document';
import { shellForPage } from '@/shell/shell';

const themeInitScript = `
(function () {
  try {
    var storedMode = window.localStorage.getItem('game-agent-theme');
    var mode = storedMode === 'light' || storedMode === 'dark' || storedMode === 'system' ? storedMode : 'system';
    var systemTheme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    var resolvedTheme = mode === 'system' ? systemTheme : mode;
    document.documentElement.dataset.themeMode = mode;
    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.style.colorScheme = resolvedTheme;
  } catch (error) {
    document.documentElement.dataset.themeMode = 'system';
    document.documentElement.dataset.theme = 'dark';
    document.documentElement.style.colorScheme = 'dark';
  }
})();
`;

/**
 * A class `_document` so the per-page shell is written into the server HTML at
 * build time. It reads the rendered page from `__NEXT_DATA__.page` and sets
 * `data-ga-shell` on `<html>`, defaulting to the current shell. Writing the
 * attribute during static prerender keeps `/` static and lets the correct
 * shell's scoped CSS apply at first paint with no client step. The Pages Router
 * hydrates into `#__next`, not `<html>`, so a server-written `<html>` attribute
 * causes no hydration mismatch.
 */
export default class GameAgentDocument extends Document<DocumentProps> {
  render() {
    const shell = shellForPage(this.props.__NEXT_DATA__.page);

    return (
      <Html lang="en" data-ga-shell={shell}>
        <Head>
          <meta name="description" content="Game Agent - AI-Powered Game Server Management" />
          <meta name="theme-color" content="#00d4ff" />
        </Head>
        <body>
          <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
          <Main />
          <NextScript />
        </body>
      </Html>
    );
  }
}
