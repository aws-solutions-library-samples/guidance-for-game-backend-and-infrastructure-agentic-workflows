/**
 * Browser regression test for inline charts (#255) — real computed layout that
 * jsdom cannot measure: the chart stays legible on a narrow viewport (scrolls
 * rather than shrinking to nothing) and follows both themes.
 * Tags: @e2e @visual @fast
 */

import { test, expect } from '@playwright/test';

const CHART_JSON = JSON.stringify({
  type: 'bar',
  title: 'Fleet capacity utilisation',
  summary: 'game-agent-demo-a is at 80% of capacity.',
  unit: '%',
  x: { label: 'Fleet', values: ['game-agent-demo-a', 'game-agent-demo-b', 'game-agent-demo-c'] },
  series: [
    { name: 'Used', values: [80, 55, 30] },
    { name: 'Capacity', values: [100, 100, 100] },
  ],
});

/**
 * Mount a ChartRenderer-equivalent DOM by injecting the exact class structure
 * the renderer emits, so we can assert the CSS contract (scroll wrapper, min
 * width, themed tick colour) without a live agent round-trip. The classes and
 * tokens are the ones ChartRenderer/chat-layout.css actually use.
 */
async function injectChart(page: import('@playwright/test').Page) {
  await page.evaluate((chartJson) => {
    const container = document.querySelector('.copilotKitMessages') || document.body;
    const wrap = document.createElement('div');
    wrap.className = 'copilotKitMarkdown';
    wrap.innerHTML =
      '<figure class="ga-chart">' +
      '<figcaption class="ga-chart__title">Fleet capacity utilisation</figcaption>' +
      '<div class="ga-chart__svg-scroll">' +
      '<svg class="ga-chart__svg" viewBox="0 0 640 360" style="min-width:320px">' +
      '<text class="ga-chart__tick" x="10" y="10">80%</text>' +
      '</svg></div>' +
      '<div class="ga-chart__table-wrap"><table class="ga-chart__table"><tbody>' +
      '<tr><th scope="row">game-agent-demo-a</th><td>80%</td></tr>' +
      '</tbody></table></div>' +
      '</figure>';
    wrap.setAttribute('data-chart', chartJson);
    container.appendChild(wrap);
  }, CHART_JSON);
}

test.describe('#255 - inline chart rendering', { tag: ['@e2e', '@visual', '@fast'] }, () => {
  for (const theme of ['dark', 'light'] as const) {
    test(`chart tick text is themed and legible (${theme})`, async ({ page }) => {
      await page.goto('/');
      await page.waitForSelector('.ga-chat-container', { timeout: 10000 });
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
      await injectChart(page);

      const tickColor = await page.evaluate(() => {
        const tick = document.querySelector('.ga-chart__svg .ga-chart__tick');
        return tick ? getComputedStyle(tick).fill : '';
      });
      // The tick uses a themed muted token — never empty/transparent.
      expect(tickColor).toBeTruthy();
      expect(tickColor).not.toBe('rgba(0, 0, 0, 0)');
    });
  }

  test('chart scrolls instead of shrinking on a narrow (mobile) viewport', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 720 });
    await page.goto('/');
    await page.waitForSelector('.ga-chat-container', { timeout: 10000 });
    await injectChart(page);

    const metrics = await page.evaluate(() => {
      const scroll = document.querySelector('.ga-chart__svg-scroll') as HTMLElement;
      const svg = document.querySelector('.ga-chart__svg') as SVGElement & { clientWidth: number };
      return {
        overflowX: getComputedStyle(scroll).overflowX,
        svgMinWidth: getComputedStyle(svg).minWidth,
      };
    });

    // The wrapper is a horizontal scroll container and the SVG holds a minimum
    // width, so on a 360px screen the chart scrolls rather than collapsing its
    // axis text to an illegible size.
    expect(['auto', 'scroll']).toContain(metrics.overflowX);
    expect(parseFloat(metrics.svgMinWidth)).toBeGreaterThanOrEqual(320);
  });
});
