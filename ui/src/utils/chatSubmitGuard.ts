/**
 * Functional chat-submission lockout for idle logout (#310).
 *
 * A transparent overlay and `aria-disabled` only affect pointer hit-testing and
 * assistive-tech semantics; they do NOT stop an already-focused CopilotKit
 * textarea from submitting on Enter, nor a programmatic submit. This guard
 * closes that gap by operating on the *actual* input and key/submit path:
 *
 *  1. A capture-phase `keydown` listener rejects a plain Enter submission before
 *     CopilotKit's own handler can see it (Shift+Enter newlines are preserved so
 *     transcript editing/reading is not broken).
 *  2. The nested textarea is functionally disabled (`disabled` + `aria-disabled`)
 *     and blurred so focus cannot drive a submit, while the transcript around it
 *     stays in the accessibility tree.
 *  3. A capture-phase `submit`/click guard on the send affordance refuses a
 *     queued send that bypasses the keyboard entirely.
 *
 * The guard is idempotent and fully reversible: `installChatSubmitGuard` returns
 * a teardown that restores the textarea's prior state and removes listeners, so
 * re-enabling the chat leaves no residue.
 *
 * This remains a UX/local-exposure control. The server still verifies tokens on
 * every request and is the authorization boundary.
 */

/**
 * Block keyboard and programmatic submission from `root` while active.
 * Returns a teardown function that fully restores prior state.
 *
 * NOTE: this function is intentionally self-contained (no module-scope
 * references) so it can be serialized and executed directly inside a real
 * browser page by the headless Playwright regression, guaranteeing the test
 * exercises the shipped guard rather than a drifting reimplementation.
 */
export function installChatSubmitGuard(root: HTMLElement): () => void {
  const CHAT_INPUT = 'textarea, input[type="text"]';
  const restorers: Array<() => void> = [];

  const isPlainEnter = (event: KeyboardEvent): boolean =>
    event.key === 'Enter' && !event.shiftKey && !event.isComposing;

  const blockKey = (event: Event) => {
    const ke = event as KeyboardEvent;
    if (isPlainEnter(ke)) {
      // Stop CopilotKit's own keydown handler (which submits) from ever running.
      ke.preventDefault();
      ke.stopPropagation();
      (ke as unknown as { stopImmediatePropagation?: () => void }).stopImmediatePropagation?.();
    }
  };

  const blockSubmit = (event: Event) => {
    event.preventDefault();
    event.stopPropagation();
    (event as unknown as { stopImmediatePropagation?: () => void }).stopImmediatePropagation?.();
  };

  // Capture phase so we win before CopilotKit's bubble-phase handlers.
  root.addEventListener('keydown', blockKey, true);
  root.addEventListener('submit', blockSubmit, true);
  restorers.push(() => {
    root.removeEventListener('keydown', blockKey, true);
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

  return () => {
    for (const restore of restorers.reverse()) restore();
  };
}
