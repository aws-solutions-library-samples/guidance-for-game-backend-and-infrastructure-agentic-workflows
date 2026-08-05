/**
 * Browser regression test for #252 — code block legibility on light theme.
 * Asserts computed styles, which jsdom cannot do.
 * Tags: @e2e @visual @fast
 */

import { test, expect } from '@playwright/test';

test.describe('#252 - code block legibility on light theme', { tag: ['@e2e', '@visual', '@fast'] }, () => {
  test('code block text has readable contrast against its background', async ({ page }) => {
    await page.goto('/');
    await page.waitForSelector('.ga-chat-container', { timeout: 10000 });

    // Force light theme the same way ThemeProvider does.
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-theme', 'light');
    });

    // Inject a CopilotKit-shaped code block into the message area so we can
    // measure the computed cascade without needing a live agent round-trip.
    // Uses the real DOM structure CopilotKit renders (copilotKitMarkdown >
    // copilotKitCodeBlock > pre > code > span with inline color).
    const colors = await page.evaluate(() => {
      const markdown = document.createElement('div');
      markdown.className = 'copilotKitMarkdown';
      markdown.innerHTML =
        '<div class="copilotKitCodeBlock"><pre><code>' +
        '<span style="color: rgb(248, 248, 242);">const x = 1;</span>' +
        '</code></pre></div>';
      document.querySelector('.copilotKitMessages')!.appendChild(markdown);

      const span = markdown.querySelector('span')!;
      const block = markdown.querySelector('.copilotKitCodeBlock')!;
      return {
        spanColor: getComputedStyle(span).color,
        blockBg: getComputedStyle(block).backgroundColor,
      };
    });

    const parseRgb = (s: string) => {
      const m = s.match(/rgba?\((\d+)[, ]+(\d+)[, ]+(\d+)/);
      return m ? { r: +m[1], g: +m[2], b: +m[3] } : null;
    };
    const luminance = ({ r, g, b }: { r: number; g: number; b: number }) =>
      0.2126 * (r / 255) + 0.7152 * (g / 255) + 0.0722 * (b / 255);

    const text = parseRgb(colors.spanColor)!;
    const bg = parseRgb(colors.blockBg)!;

    // Pre-fix: text computed to --ga-text (#182033, luminance ~0.02) on the
    // near-black block (rgb(9 9 11)) — both dark, unreadable. The fix forces
    // light text; require a real luminance gap.
    expect(Math.abs(luminance(text) - luminance(bg))).toBeGreaterThan(0.4);
  });
});
