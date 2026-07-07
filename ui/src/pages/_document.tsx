import { Html, Head, Main, NextScript } from 'next/document';

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

export default function Document() {
  return (
    <Html lang="en">
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
