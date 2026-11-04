/**
 * Focus-trap helper for the idle warning dialog (#310).
 *
 * The original trap only considered *enabled* buttons and bailed out when none
 * existed. While a refresh is in flight both dialog buttons are disabled, so the
 * trap had zero targets and Tab escaped to the page behind the modal. This
 * helper always includes the dialog container itself (made focusable via
 * `tabindex="-1"`) as a guaranteed in-dialog fallback, so focus is contained
 * even in the zero-enabled-action busy state.
 *
 * Kept dependency-free and self-contained so the shipped logic can be executed
 * directly inside a real browser by the headless focus-trap regression.
 */

/**
 * Ordered focus targets inside the dialog. Enabled controls come first; the
 * focusable container is always appended as a fallback so the list is never
 * empty while the dialog is open.
 */
export function getFocusTrapTargets(container: HTMLElement): HTMLElement[] {
  const enabled = Array.from(container.querySelectorAll<HTMLElement>('button:not([disabled])'));
  // The container carries tabindex="-1"; include it so a busy dialog (all
  // actions disabled) still has an in-dialog target to hold focus.
  return enabled.length > 0 ? enabled : [container];
}

/**
 * Handle a Tab/Shift+Tab keydown to keep focus wrapped inside the dialog.
 * Returns true if the event was handled (and default prevented).
 *
 * Self-contained (inlines target computation) so it can be serialized and run
 * inside a real browser by the headless focus-trap regression, guaranteeing the
 * test exercises the shipped trap rather than a reimplementation.
 */
export function handleFocusTrapKeydown(container: HTMLElement, event: KeyboardEvent): boolean {
  if (event.key !== 'Tab') return false;
  const enabled = Array.from(container.querySelectorAll<HTMLElement>('button:not([disabled])'));
  const targets: HTMLElement[] = enabled.length > 0 ? enabled : [container];
  const first = targets[0];
  const last = targets[targets.length - 1];
  const active = document.activeElement;

  // Zero-enabled-action state: only the container is a target. Any Tab keeps
  // focus pinned to the container instead of leaving the modal.
  if (targets.length === 1) {
    event.preventDefault();
    first.focus();
    return true;
  }

  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

/**
 * Choose the element to focus when the dialog opens or its busy state changes:
 * the preferred control if enabled, otherwise the first available target
 * (which falls back to the focusable container while busy).
 */
export function initialFocusTarget(
  container: HTMLElement,
  preferred: HTMLElement | null,
): HTMLElement {
  if (preferred && !preferred.hasAttribute('disabled')) return preferred;
  return getFocusTrapTargets(container)[0];
}
