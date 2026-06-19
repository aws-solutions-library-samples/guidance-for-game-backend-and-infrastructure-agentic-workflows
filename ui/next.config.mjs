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
    return [
      {
        source: '/(.*)',
        headers: [
          {
            key: 'X-Frame-Options',
            value: 'DENY',
          },
        ],
      },
    ]
  },
}

export default nextConfig
