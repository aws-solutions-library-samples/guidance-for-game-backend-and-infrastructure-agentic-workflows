const VISIBLE_RUNTIME_ERROR =
  /identity verification issue|internal server error|traceback|exception|failed to|could not|couldn['’]t|cannot|can't|(?:don['’]t|do not) have access|access denied|not authorized|unauthorized|timed? out|timeout|throttl|temporarily unavailable|no live data|did not (?:check|retrieve|list|query|inspect)|not checked|not able to (?:reach|retrieve|list|query|inspect)|unable to (?:reach|retrieve|list|query|inspect)|mcp .*error|something went wrong|\b503\b|\b502\b/i;

export function assertHealthySpecialistReply(reply: string, expectedTerms: string[], specialist: string): void {
  const normalized = reply.trim();
  if (!normalized || VISIBLE_RUNTIME_ERROR.test(normalized)) {
    throw new Error(`${specialist} reply surfaced a runtime error: ${normalized.slice(0, 200)}`);
  }

  const lower = normalized.toLowerCase();
  if (!expectedTerms.some((term) => lower.includes(term.toLowerCase()))) {
    throw new Error(
      `${specialist} reply looks off-topic (expected one of ${JSON.stringify(expectedTerms)}): ` +
        normalized.slice(0, 200),
    );
  }
}
