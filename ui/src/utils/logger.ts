/**
 * Logger utility for Game Agent frontend.
 */

// Collapse carriage returns, line feeds, and other C0/DEL control characters to
// a single space. An externally influenced value (a request id, thread id, or
// error text) containing CR/LF could otherwise forge an additional,
// attacker-controlled log line in the aggregated sink.
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/g;

function normalizeLogValue(message: string): string {
  return message.replace(CONTROL_CHARS, ' ');
}

// Messages are passed as a separate argument behind a fixed "%s" format
// specifier so externally-influenced content can never be interpreted as a
// format string (CodeQL: js/tainted-format-string). Control characters are
// normalized first so one field cannot become multiple log lines.
export function logInfo(message: string): void {
  console.log('%s', normalizeLogValue(message));
}

export function logError(message: string, error?: Error): void {
  console.error('%s', normalizeLogValue(message), error);
}

export function logWarning(message: string): void {
  console.warn('%s', normalizeLogValue(message));
}

export function logDebug(message: string): void {
  // Debug logs are typically disabled in production
  if (process.env.NODE_ENV === 'development') {
    console.log(`[DEBUG] ${normalizeLogValue(message)}`);
  }
}

/**
 * Redact a PII/identifier value for safe logging at info level.
 *
 * Keeps a short non-reversible prefix for correlation while not writing the full
 * email / user id / session id to logs (which land in CloudWatch). Control
 * characters are stripped so a crafted value cannot inject a log line, and the
 * kept prefix is drawn only from leading non-control characters. Returns a fixed
 * placeholder for empty values. Example: "user@example.com" -> "us…(redacted)".
 */
export function redact(value: string | undefined | null): string {
  if (!value) return '<none>';
  const safe = normalizeLogValue(value).trimStart();
  if (!safe) return '<none>';
  const head = safe.slice(0, 2);
  return `${head}…(redacted)`;
}
