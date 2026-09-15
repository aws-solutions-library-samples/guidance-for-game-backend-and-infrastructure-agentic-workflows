import { expect, Page } from '@playwright/test';

export { assertHealthySpecialistReply } from './reply-contract';

export const ASSISTANT_MESSAGE_SELECTOR = '.copilotKitAssistantMessage';

/** Send one user turn and return the newly-created, settled assistant reply. */
export async function sendAndAwaitReply(page: Page, query: string, timeoutMs = 180_000): Promise<string> {
  const before = await page.locator(ASSISTANT_MESSAGE_SELECTOR).count();
  const chatInput = page.locator('textarea').first();
  await expect(chatInput).toBeVisible();
  await chatInput.fill(query);
  await chatInput.press('Enter');

  await expect
    .poll(async () => page.locator(ASSISTANT_MESSAGE_SELECTOR).count(), {
      timeout: timeoutMs,
      intervals: [2_000],
    })
    .toBeGreaterThan(before);

  const reply = page.locator(ASSISTANT_MESSAGE_SELECTOR).last();
  let previous = '';
  await expect
    .poll(
      async () => {
        const current = (await reply.innerText()).trim();
        const settled = current.length > 20 && current === previous;
        previous = current;
        return settled;
      },
      { timeout: timeoutMs, intervals: [3_000] },
    )
    .toBe(true);

  return previous;
}
