/**
 * The empty authenticated Cloudscape shell (foundation for the Cloudscape
 * redesign). It renders an accessible `AppLayout` with a top navigation and an
 * empty content region in light or dark mode. The full chat, chart, and
 * operator surfaces are added by later changes; this module establishes the
 * shell, the global-styles import, and the theme bridge.
 *
 * This module and `@cloudscape-design/global-styles` are imported only from the
 * Cloudscape page entry (through `next/dynamic`), never from `_app` or any
 * current-UI page, so no Cloudscape code or global CSS reaches the current UI's
 * bundle.
 */

// Third-party packages
import { useEffect, useLayoutEffect, useRef } from 'react';
import '@cloudscape-design/global-styles/index.css';
import AppLayout from '@cloudscape-design/components/app-layout';
import ContentLayout from '@cloudscape-design/components/content-layout';
import Header from '@cloudscape-design/components/header';
import TopNavigation from '@cloudscape-design/components/top-navigation';

// Local modules
import { useTheme } from '../ThemeProvider';
import { applyCloudscapeMode, applyGaTokenBridge } from '@/shell/cloudscapeThemeBridge';

/**
 * Run the theme bridge before the browser paints so the first frame already
 * carries the correct Cloudscape visual mode and bridged tokens. `useEffect`
 * on the server is a no-op, so fall back to it when `useLayoutEffect` would warn
 * during a server render.
 */
const useIsomorphicLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect;

export default function CloudscapeShell() {
  const { resolvedTheme } = useTheme();
  const rootRef = useRef<HTMLDivElement>(null);

  useIsomorphicLayoutEffect(() => {
    applyCloudscapeMode(resolvedTheme);
    // Write the --ga-* bridge onto document.body (where Cloudscape's base theme
    // tokens live) so first-party component CSS resolves to Cloudscape colors
    // everywhere, including chrome such as IdleWarningDialog that renders
    // outside this shell's root element. Also bridge the shell root so a nested
    // element resolves the same values without walking up to <body>.
    applyGaTokenBridge(document.body);
    applyGaTokenBridge(rootRef.current);
  }, [resolvedTheme]);

  return (
    <div ref={rootRef}>
      <div id="ga-top-navigation">
        <TopNavigation
          identity={{ href: '/', title: 'Game Agent' }}
          utilities={[]}
          i18nStrings={{ overflowMenuTriggerText: 'More', overflowMenuTitleText: 'All' }}
        />
      </div>
      <AppLayout
        navigationHide
        toolsHide
        // Point AppLayout at the TopNavigation wrapper so it subtracts the
        // navigation height from its own; without this the empty layout is
        // sized to the full viewport beneath the navigation and overflows.
        headerSelector="#ga-top-navigation"
        ariaLabels={{ navigation: 'Navigation', tools: 'Tools' }}
        content={<ContentLayout header={<Header variant="h1">Game Agent</Header>} />}
      />
    </div>
  );
}
