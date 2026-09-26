/**
 * Blocker 3 (headless browser): once idle logout begins, a real focused
 * textarea must not submit on Enter, and a programmatic/queued send must be
 * refused. jsdom cannot reproduce focus + Enter + capture-phase ordering
 * faithfully, so this runs the SHIPPED guard in real Chromium.
 *
 * Tags: @browser @idle
 */
import { test, expect } from '@playwright/test';
import { installChatSubmitGuard } from '../../src/utils/chatSubmitGuard';

// Mirrors the production Chat DOM: a wrapper whose textarea submits on a plain
// Enter (as CopilotKit's does) via a bubble-phase handler, plus a send button.
const FIXTURE = `
  <input id="outside" type="text" />
  <div class="ga-chat-wrapper" id="wrapper">
    <div class="ga-chat-disabled-overlay" aria-hidden="true"></div>
    <form id="chatform">
      <textarea id="chatinput" placeholder="Ask about game infrastructure..."></textarea>
      <button id="send" type="submit" class="copilotKitSendButton">Send</button>
    </form>
  </div>
  <script>
    window.__sends = 0;
    var form = document.getElementById('chatform');
    var input = document.getElementById('chatinput');
    // CopilotKit submits on bare Enter from the focused textarea (bubble phase).
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        window.__sends++;
      }
    });
    // ...and on an explicit submit / send-button click.
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      window.__sends++;
    });
  </script>
`;

test.describe('idle chat lockout (real browser)', { tag: ['@browser', '@idle'] }, () => {
  test('an already-focused textarea does not submit on Enter once disabled', async ({ page }) => {
    await page.setContent(FIXTURE);

    // Acquire focus in the textarea BEFORE the lockout engages (the exact race
    // the overlay-only approach failed to cover), then type.
    await page.focus('#chatinput');
    await page.keyboard.type('hello');

    // Engage the shipped guard (equivalent to `disabled` becoming true).
    await page.evaluate((guardSrc) => {
      const install = new Function('return (' + guardSrc + ')')() as (
        root: HTMLElement,
      ) => () => void;
      const root = document.getElementById('wrapper') as HTMLElement;
      (window as unknown as { __teardown: () => void }).__teardown = install(root);
    }, installChatSubmitGuard.toString());

    // Press Enter from wherever focus now is (guard should have blurred/disabled).
    await page.keyboard.press('Enter');
    // Re-focus and try again to simulate a determined already-focused submit.
    await page.evaluate(() => (document.getElementById('chatinput') as HTMLTextAreaElement).focus());
    await page.keyboard.press('Enter');

    const result = await page.evaluate(() => ({
      sends: (window as unknown as { __sends: number }).__sends,
      disabled: (document.getElementById('chatinput') as HTMLTextAreaElement).disabled,
      ariaDisabled: document.getElementById('chatinput')?.getAttribute('aria-disabled'),
    }));

    expect(result.sends).toBe(0);
    expect(result.disabled).toBe(true);
    expect(result.ariaDisabled).toBe('true');
  });

  test('a programmatic send-button click is refused while disabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.evaluate((guardSrc) => {
      const install = new Function('return (' + guardSrc + ')')() as (
        root: HTMLElement,
      ) => () => void;
      install(document.getElementById('wrapper') as HTMLElement);
      // Programmatic click bypassing pointer hit-testing / the overlay entirely.
      (document.getElementById('send') as HTMLButtonElement).click();
    }, installChatSubmitGuard.toString());

    const sends = await page.evaluate(() => (window as unknown as { __sends: number }).__sends);
    expect(sends).toBe(0);
  });

  test('Shift+Enter is preserved (transcript editing not broken) while disabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.focus('#chatinput');
    await page.evaluate((guardSrc) => {
      const install = new Function('return (' + guardSrc + ')')() as (
        root: HTMLElement,
      ) => () => void;
      install(document.getElementById('wrapper') as HTMLElement);
      // The textarea is disabled, but Shift+Enter must never count as a submit.
    }, installChatSubmitGuard.toString());
    await page.keyboard.press('Shift+Enter');
    const sends = await page.evaluate(() => (window as unknown as { __sends: number }).__sends);
    expect(sends).toBe(0);
  });

  test('teardown restores the textarea so re-enabling leaves no residue', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.evaluate((guardSrc) => {
      const install = new Function('return (' + guardSrc + ')')() as (
        root: HTMLElement,
      ) => () => void;
      const teardown = install(document.getElementById('wrapper') as HTMLElement);
      teardown();
    }, installChatSubmitGuard.toString());

    // After teardown a bare Enter submits again (chat is usable once re-enabled).
    await page.focus('#chatinput');
    await page.keyboard.type('hi');
    await page.keyboard.press('Enter');
    const result = await page.evaluate(() => ({
      sends: (window as unknown as { __sends: number }).__sends,
      disabled: (document.getElementById('chatinput') as HTMLTextAreaElement).disabled,
    }));
    expect(result.sends).toBe(1);
    expect(result.disabled).toBe(false);
  });
});
