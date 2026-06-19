/** @type {import('next').NextConfig} */
const nextConfig = {
  // React strict mode for better development experience
  reactStrictMode: true,

  // Standalone output for Docker/container deployment
  output: 'standalone',

  // Note: instrumentation.ts (OpenTelemetry/ADOT) auto-loads in Next 16;
  // the former experimental.instrumentationHook flag was removed upstream.

  // Transpile CopilotKit packages (required for proper bundling)
  transpilePackages: [
    '@copilotkit/react-core',
    '@copilotkit/react-ui',
    '@copilotkit/react-textarea'
  ],

  // Strict type checking (DO NOT disable in production)
  typescript: {
    ignoreBuildErrors: false,
  },

  // Strict ESLint checking (DO NOT disable in production)
  eslint: {
    ignoreDuringBuilds: false,
  },

  // Security headers for production
  async headers() {
    // Content Security Policy. CopilotKit's chat UI uses inline styles and the
    // Next runtime needs inline/eval scripts, so 'unsafe-inline'/'unsafe-eval'
    // are required for script/style; everything else is locked to 'self'.
    // connect-src allows the app's own API routes (which proxy to AWS) + AWS
    // endpoints reached from the browser (Cognito, etc.).
    const csp = [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "object-src 'none'",
    ].join('; ')

    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
          { key: 'Strict-Transport-Security', value: 'max-age=63072000; includeSubDomains; preload' },
          { key: 'Content-Security-Policy', value: csp },
        ],
      },
    ]
  },
}

export default nextConfig
