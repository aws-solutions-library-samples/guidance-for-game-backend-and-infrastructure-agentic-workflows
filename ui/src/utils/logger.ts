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
