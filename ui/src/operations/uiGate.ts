/**
 * Compile/runtime gate for the optional operator console.
 *
 * The surface is disabled unless the public build setting is exactly `true`.
 * Backend authorization remains authoritative when enabled; this gate only
 * controls whether the product exposes the page, navigation, and discovery
 * traffic in a given frontend build.
 */
export function operationsUiEnabled(): boolean {
  return process.env.NEXT_PUBLIC_OPERATIONS_UI_ENABLED === 'true';
}
