/**
 * Functional chat-submission lockout for idle logout (#310).
 *
 * A transparent overlay and `aria-disabled` only affect pointer hit-testing and
 * assistive-tech semantics; they do NOT stop an already-focused CopilotKit
 * textarea from submitting, nor a programmatic send-button click.
 *
 * The installed `@copilotkit/react-ui` 1.10.6 sender is a PLAIN button with
 * `onClick={send}` (see node_modules/@copilotkit/react-ui/src/components/chat/
 * Input.tsx). It is NOT a form submit: pressing Enter in the textarea calls
 * `send()` directly from an `onKeyDown` handler, and the send button is a native
 * `<button>` whose click fires `send`. Native buttons also activate their
 * onClick on Space/Enter when focused. A submit-only guard therefore misses
 * every real send path.
 *
 * This guard closes that gap by operating on the *actual* input and the real
 * key/click paths:
 *
 *  1. A capture-phase `keydown` listener rejects a plain Enter (and a Space/Enter
 *     activation while focus is on a button) before CopilotKit's own handler can
 *     see it. Shift+Enter newlines are preserved so transcript editing/reading
 *     is not broken.
 *  2. A capture-phase `click` listener refuses any send-button click —
 *     `button.click()`, pointer, or keyboard-synthesized — that would otherwise
 *     reach CopilotKit's `onClick={send}`.
 *  3. A capture-phase `submit` guard remains for any form-wrapped sender.
 *  4. The nested textarea AND the send button are functionally disabled
 *     (`disabled` + `aria-disabled`) and the textarea is blurred, while the
 *     transcript around them stays in the accessibility tree.
 *
 * The guard is idempotent and fully reversible: `installChatSubmitGuard` returns
 * a teardown that restores prior state and removes listeners, so re-enabling the
 * chat leaves no residue.
 *
 * This remains a UX/local-exposure control. The server still verifies tokens on
 * every request and is the authorization boundary.
 *
 * NOTE: this function is intentionally self-contained (no module-scope
 * references) so it can be serialized and executed directly inside a real
 * browser page by the headless Playwright regression, guaranteeing the test
 * exercises the shipped guard rather than a drifting reimplementation.
 */
export function installChatSubmitGuard(root: HTMLElement): () => void {
  const CHAT_INPUT = 'textarea, input[type="text"]';
  // The real CopilotKit sender + common fallbacks. `copilotKitInputControlButton`
  // is the plain send button in 1.10.6; the others cover form-based/legacy DOM.
  const SEND_BUTTON =
    '.copilotKitInputControlButton, .copilotKitSendButton, button[type="submit"], button[data-testid="chat-send"]';
  const restorers: Array<() => void> = [];

  const isPlainEnter = (event: KeyboardEvent): boolean =>
    event.key === 'Enter' && !event.shiftKey && !event.isComposing;

  const isButton = (node: EventTarget | null): boolean =>
    node instanceof HTMLElement && !!node.closest('button');

  const blockKey = (event: Event) => {
    const ke = event as KeyboardEvent;
    // A plain Enter anywhere in the chat (textarea send OR button activation).
    // Also block Space/Enter activation while a button is focused, since native
    // button activation would fire the plain onClick sender.
    const activatingButton =
      (ke.key === ' ' || ke.key === 'Spacebar' || ke.key === 'Enter') && isButton(ke.target);
    if (isPlainEnter(ke) || activatingButton) {
      ke.preventDefault();
      ke.stopPropagation();
      (ke as unknown as { stopImmediatePropagation?: () => void }).stopImmediatePropagation?.();
    }
  };

  const blockClick = (event: Event) => {
    // Refuse any click that reaches a button (the plain onClick sender), whether
    // from a real pointer, a keyboard activation, or a programmatic
    // `button.click()`.
    if (isButton(event.target)) {
      event.preventDefault();
      event.stopPropagation();
      (event as unknown as { stopImmediatePropagation?: () => void }).stopImmediatePropagation?.();
    }
  };

  const blockSubmit = (event: Event) => {
    event.preventDefault();
    event.stopPropagation();
    (event as unknown as { stopImmediatePropagation?: () => void }).stopImmediatePropagation?.();
  };

  // Capture phase so we win before CopilotKit's bubble-phase handlers.
  root.addEventListener('keydown', blockKey, true);
  root.addEventListener('click', blockClick, true);
  root.addEventListener('submit', blockSubmit, true);
  restorers.push(() => {
    root.removeEventListener('keydown', blockKey, true);
    root.removeEventListener('click', blockClick, true);
    root.removeEventListener('submit', blockSubmit, true);
  });

  // Functionally disable the actual input(s): disabled + blurred so an
  // already-focused textarea cannot submit, but still present for AT reading.
  const inputs = Array.from(root.querySelectorAll<HTMLTextAreaElement | HTMLInputElement>(CHAT_INPUT));
  for (const input of inputs) {
    const hadDisabled = input.hasAttribute('disabled');
    const priorAriaDisabled = input.getAttribute('aria-disabled');
    input.setAttribute('aria-disabled', 'true');
    input.disabled = true;
    if (document.activeElement === input) {
      input.blur();
    }
    restorers.push(() => {
      if (!hadDisabled) input.disabled = false;
      if (priorAriaDisabled === null) input.removeAttribute('aria-disabled');
      else input.setAttribute('aria-disabled', priorAriaDisabled);
    });
  }

  // Functionally disable the actual send button so React state cannot keep it
  // enabled after the textarea is disabled. A disabled button ignores clicks and
  // keyboard activation entirely.
  const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>(SEND_BUTTON));
  for (const button of buttons) {
    const hadDisabled = button.hasAttribute('disabled');
    const priorAriaDisabled = button.getAttribute('aria-disabled');
    button.setAttribute('aria-disabled', 'true');
    button.disabled = true;
    if (document.activeElement === button) {
      button.blur();
    }
    restorers.push(() => {
      if (!hadDisabled) button.disabled = false;
      if (priorAriaDisabled === null) button.removeAttribute('aria-disabled');
      else button.setAttribute('aria-disabled', priorAriaDisabled);
    });
  }

  return () => {
    for (const restore of restorers.reverse()) restore();
  };
}
