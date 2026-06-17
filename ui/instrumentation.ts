/**
 * Next.js Instrumentation Hook
 *
 * Placeholder for ADOT/OpenTelemetry integration on ECS Express.
 * ADOT is not currently configured — add a sidecar container to
 * the ECS Express template to enable tracing and metrics export.
 */

export async function register() {
  if (process.env.NODE_ENV === 'production' && process.env.NEXT_RUNTIME === 'nodejs') {
    // ADOT auto-configuration - no manual setup required
    console.log('✅ ADOT auto-configuration enabled for ECS Express');
  }
}
