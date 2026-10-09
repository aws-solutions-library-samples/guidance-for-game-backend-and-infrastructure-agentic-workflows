/**
 * Logger utility for Game Agent frontend.
 */

// Collapse carriage returns, line feeds, and other control characters to a
// single space. An externally influenced value (a request id, thread id, or
// error text) containing CR/LF could otherwise forge an additional,
// attacker-controlled log line in the aggregated sink. The class covers all C0
// controls and DEL, the full C1 range, the Unicode line and paragraph
// separators (U+2028/U+2029, which Unicode-aware consumers treat as line
// breaks), and the bidirectional-formatting controls (U+202A–U+202E,
// U+2066–U+2069) that can reorder how a line renders to hide injected content.
const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

export function normalizeLogValue(value: unknown): string {
  return String(value).replace(CONTROL_CHARS, ' ');
}

// Messages are passed as a separate argument behind a fixed "%s" format
// specifier so externally-influenced content can never be interpreted as a
// format string (CodeQL: js/tainted-format-string). Control characters are
// normalized first so one field cannot become multiple log lines.
export function logInfo(message: string): void {
  console.log('%s', normalizeLogValue(message));
}

// An Error object reaching the sink would print its message and stack verbatim;
// an AWS SDK error message, for example, can name the caller ARN. In production
// only a stable error identifier (name or concrete class name) and safe
// diagnostic codes (AWS SDK HTTP status, and a `code`/`cause.code` matching a
// fixed uppercase token pattern) are emitted; the full stack is kept only in
// development. Everything is forwarded behind the fixed "%s" specifier and
// normalized.
export function logError(message: string, error?: unknown): void {
  if (error === undefined) {
    console.error('%s', normalizeLogValue(message));
    return;
  }
  console.error('%s', normalizeLogValue(message), normalizeLogValue(describeError(error)));
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

// Render an error for logging without disclosing provider payloads. In
// development the stack aids debugging; in production only a stable error
// identifier and safe diagnostic codes are kept: the error name (falling back
// to the concrete class name when `name` is the generic "Error", so an
// aws-jwt-verify error like JwtExpiredError is still distinguishable), the AWS
// SDK HTTP status code, and a `code`/`cause.code` that matches a fixed
// uppercase token pattern (for example ERR_JWT_EXPIRED or ECONNREFUSED). None
// of these carry caller values or free-form provider text.
const SAFE_CODE = /^[A-Z][A-Z0-9_]{1,40}$/;

function describeError(error: unknown): string {
  if (process.env.NODE_ENV === 'development') {
    if (error instanceof Error) {
      return error.stack ?? `${error.name}: ${error.message}`;
    }
    return String(error);
  }
  if (error instanceof Error) {
    // Prefer a specific name: subclasses whose `name` was never overridden
    // report the generic "Error", so fall back to the concrete constructor name.
    let label = error.name;
    if (!label || label === 'Error') {
      label = error.constructor?.name || 'Error';
    }
    const parts: string[] = [label];
    const status = (error as { $metadata?: { httpStatusCode?: number } }).$metadata?.httpStatusCode;
    if (status !== undefined) {
      parts.push(`httpStatusCode=${status}`);
    }
    const code = (error as { code?: unknown }).code;
    if (typeof code === 'string' && SAFE_CODE.test(code)) {
      parts.push(`code=${code}`);
    }
    const causeCode = (error as { cause?: { code?: unknown } }).cause?.code;
    if (typeof causeCode === 'string' && SAFE_CODE.test(causeCode)) {
      parts.push(`cause.code=${causeCode}`);
    }
    return parts.length > 1 ? `${parts[0]} (${parts.slice(1).join(', ')})` : parts[0];
  }
  return typeof error;
}

/**
 * Redact a PII/identifier value for safe logging at info level.
 *
 * Keeps a short non-reversible prefix for correlation while not writing the full
 * email / user id / session id to logs (which land in CloudWatch). Accepts any
 * value and coerces it to a string so a non-string caller value (for example a
 * numeric thread id in request JSON) never throws. Control characters are
 * stripped so a crafted value cannot inject a log line, and the kept prefix is
 * drawn only from leading non-control characters. Returns a fixed placeholder
 * for empty values. Example: "user@example.com" -> "us…(redacted)".
 */
export function redact(value: unknown): string {
  if (value === null || value === undefined || value === '') return '<none>';
  const safe = normalizeLogValue(value).trimStart();
  if (!safe) return '<none>';
  const head = safe.slice(0, 2);
  return `${head}…(redacted)`;
}
