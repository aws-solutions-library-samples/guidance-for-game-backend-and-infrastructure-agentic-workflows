/**
 * Logger utility for Game Agent frontend.
 */

// Messages are passed as a separate argument behind a fixed "%s" format
// specifier so externally-influenced content can never be interpreted as a
// format string (CodeQL: js/tainted-format-string).
export function logInfo(message: string): void {
  console.log('%s', message);
}

export function logError(message: string, error?: Error): void {
  console.error('%s', message, error);
}

export function logWarning(message: string): void {
  console.warn('%s', message);
}

export function logDebug(message: string): void {
  // Debug logs are typically disabled in production
  if (process.env.NODE_ENV === 'development') {
    console.log(`[DEBUG] ${message}`);
  }
}

/**
 * Redact a PII/identifier value for safe logging at info level.
 *
 * Keeps a short non-reversible prefix for correlation while not writing the full
 * email / user id / session id to logs (which land in CloudWatch). Returns a
 * fixed placeholder for empty values. Example: "jp.velasco@example.com" -> "jp…(redacted)".
 */
export function redact(value: string | undefined | null): string {
  if (!value) return '<none>';
  const head = value.slice(0, 2);
  return `${head}…(redacted)`;
}
