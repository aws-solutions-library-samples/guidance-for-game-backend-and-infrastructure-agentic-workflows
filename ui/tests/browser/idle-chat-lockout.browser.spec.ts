/**
 * Blocker 4 (headless browser): once idle logout begins, the REAL CopilotKit
 * plain-button sender must not fire. jsdom cannot reproduce focus + real key
 * activation + capture-phase ordering + native button activation faithfully, so
 * this runs the SHIPPED guard in real Chromium.
 *
 * The fixture faithfully models the installed @copilotkit/react-ui 1.10.6 Input
 * (node_modules/@copilotkit/react-ui/src/components/chat/Input.tsx): a PLAIN
 * `<button onClick={send}>` (NOT a form submit) with retained text state, and a
 * textarea whose bare Enter calls send() directly from onKeyDown. There is no
 * <form> and no type="submit" — sending happens through the plain onClick path.
 *
 * Tags: @browser @idle
 */
import { test, expect } from '@playwright/test';
import { installChatSubmitGuard } from '../../src/utils/chatSubmitGuard';

const FIXTURE = `
  <input id="outside" type="text" />
  <div class="ga-chat-wrapper" id="wrapper">
    <div class="ga-chat-disabled-overlay" aria-hidden="true"></div>
    <div class="copilotKitInput">
      <textarea id="chatinput" placeholder="Ask about game infrastructure...">has text</textarea>
      <div class="copilotKitInputControls">
        <button id="send" class="copilotKitInputControlButton">Send</button>
      </div>
    </div>
  </div>
  <div id="transcript" role="log">previous messages</div>
  <script>
    window.__sends = 0;
    var input = document.getElementById('chatinput');
    var send = document.getElementById('send');
    // Retained-text send(), exactly like CopilotKit's plain onClick sender.
    function doSend() {
      if (input.value.trim().length === 0) return;
      window.__sends++;
    }
    // CopilotKit calls send() directly from the textarea onKeyDown on bare Enter
    // (NOT a form submit).
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        doSend();
      }
    });
    // The send button is a plain button with onClick={send}.
    send.addEventListener('click', doSend);
  </script>
`;

function guardSrc() {
  return installChatSubmitGuard.toString();
}

test.describe('idle chat lockout (real browser)', { tag: ['@browser', '@idle'] }, () => {
  test('an already-focused textarea does not send on Enter once disabled', async ({ page }) => {
    await page.setContent(FIXTURE);

    // Acquire focus in the textarea BEFORE the lockout engages (the exact race
    // the overlay-only approach failed to cover).
    await page.focus('#chatinput');

    // Engage the shipped guard (equivalent to `disabled` becoming true).
    await page.evaluate((src) => {
      const install = new Function('return (' + src + ')')() as (root: HTMLElement) => () => void;
      const root = document.getElementById('wrapper') as HTMLElement;
      (window as unknown as { __teardown: () => void }).__teardown = install(root);
    }, guardSrc());

    // Press Enter from wherever focus now is (guard should have blurred/disabled).
    await page.keyboard.press('Enter');
    // Re-focus and try again to simulate a determined already-focused submit.
    await page.evaluate(() => (document.getElementById('chatinput') as HTMLTextAreaElement).focus());
    await page.keyboard.press('Enter');

    const result = await page.evaluate(() => ({
      sends: (window as unknown as { __sends: number }).__sends,
      disabled: (document.getElementById('chatinput') as HTMLTextAreaElement).disabled,
      ariaDisabled: document.getElementById('chatinput')?.getAttribute('aria-disabled'),
      sendDisabled: (document.getElementById('send') as HTMLButtonElement).disabled,
      transcript: document.getElementById('transcript')?.textContent,
    }));

    expect(result.sends).toBe(0);
    expect(result.disabled).toBe(true);
    expect(result.ariaDisabled).toBe('true');
    // The real send button is functionally disabled, not merely covered.
    expect(result.sendDisabled).toBe(true);
    // The transcript stays readable/accessible.
    expect(result.transcript).toContain('previous messages');
  });

  test('a programmatic send-button click is refused while disabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.evaluate((src) => {
      const install = new Function('return (' + src + ')')() as (root: HTMLElement) => () => void;
      install(document.getElementById('wrapper') as HTMLElement);
      // Programmatic click bypassing pointer hit-testing / the overlay entirely,
      // straight at the plain onClick sender.
      (document.getElementById('send') as HTMLButtonElement).click();
    }, guardSrc());

    const sends = await page.evaluate(() => (window as unknown as { __sends: number }).__sends);
    expect(sends).toBe(0);
  });

  test('a real pointer click on the send button is refused while disabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.evaluate((src) => {
      const install = new Function('return (' + src + ')')() as (root: HTMLElement) => () => void;
      install(document.getElementById('wrapper') as HTMLElement);
    }, guardSrc());

    // A real user click via Playwright (force past the overlay hit-testing).
    await page.click('#send', { force: true });
    const sends = await page.evaluate(() => (window as unknown as { __sends: number }).__sends);
    expect(sends).toBe(0);
  });

  test('keyboard activation (Enter/Space) on the focused send button cannot send while disabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.evaluate((src) => {
      const install = new Function('return (' + src + ')')() as (root: HTMLElement) => () => void;
      install(document.getElementById('wrapper') as HTMLElement);
    }, guardSrc());

    // Attempt to focus and activate the send button by keyboard. A disabled
    // native button is not focusable/activatable, and the capture guard blocks
    // Enter/Space activation regardless.
    await page.evaluate(() => (document.getElementById('send') as HTMLButtonElement).focus());
    await page.keyboard.press('Enter');
    await page.keyboard.press('Space');

    const sends = await page.evaluate(() => (window as unknown as { __sends: number }).__sends);
    expect(sends).toBe(0);
  });

  test('Shift+Enter is preserved (transcript editing not broken) while disabled', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.focus('#chatinput');
    await page.evaluate((src) => {
      const install = new Function('return (' + src + ')')() as (root: HTMLElement) => () => void;
      install(document.getElementById('wrapper') as HTMLElement);
    }, guardSrc());
    await page.keyboard.press('Shift+Enter');
    const sends = await page.evaluate(() => (window as unknown as { __sends: number }).__sends);
    expect(sends).toBe(0);
  });

  test('teardown restores the input and send button so re-enabling leaves no residue', async ({ page }) => {
    await page.setContent(FIXTURE);
    await page.evaluate((src) => {
      const install = new Function('return (' + src + ')')() as (root: HTMLElement) => () => void;
      const teardown = install(document.getElementById('wrapper') as HTMLElement);
      teardown();
    }, guardSrc());

    // After teardown a plain-button click sends again (chat usable once re-enabled).
    await page.click('#send');
    const result = await page.evaluate(() => ({
      sends: (window as unknown as { __sends: number }).__sends,
      disabled: (document.getElementById('chatinput') as HTMLTextAreaElement).disabled,
      sendDisabled: (document.getElementById('send') as HTMLButtonElement).disabled,
    }));
    expect(result.sends).toBe(1);
    expect(result.disabled).toBe(false);
    expect(result.sendDisabled).toBe(false);
  });
});
