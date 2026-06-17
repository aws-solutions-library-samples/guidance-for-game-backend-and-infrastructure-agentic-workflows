/**
 * Logger utility for Game Agent frontend.
 */

export function logInfo(message: string): void {
  console.log(message);
}

export function logError(message: string, error?: Error): void {
  console.error(message, error);
}

export function logWarning(message: string): void {
  console.warn(message);
}

export function logDebug(message: string): void {
  // Debug logs are typically disabled in production
  if (process.env.NODE_ENV === 'development') {
    console.log(`[DEBUG] ${message}`);
  }
}
