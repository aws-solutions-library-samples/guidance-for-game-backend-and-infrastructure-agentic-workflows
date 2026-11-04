/**
 * Frontend AWS SDK dependency least-privilege guard (issue #458).
 *
 * WHY THIS EXISTS: the frontend is a Next.js proxy. Its only AWS SDK callers are
 *   - `@aws-sdk/client-sts`  -> GetCallerIdentity (build AgentCore runtime ARN,
 *                               dev identity) in pages/api/copilot/chat.ts
 *   - `@aws-sdk/client-cognito-identity-provider` -> auth sign-in and token
 *                               refresh in pages/api/auth/*
 *   - `aws-jwt-verify`       -> Cognito JWT verification (no IAM, HTTPS JWKS)
 * AgentCore is invoked with a Cognito JWT bearer token over `fetch`, NOT the AWS
 * SDK, so the Bedrock Runtime client is not a caller. CloudWatch, Cost Explorer,
 * EKS, and GameLift are backend/AgentCore specialist concerns with no frontend
 * caller.
 *
 * Per least-privilege dependency hygiene, an unused client SDK is unnecessary
 * attack surface and dependency-update burden. This test fails if any
 * never-imported SDK client is re-added to package.json, and verifies that each
 * retained SDK both stays declared AND has a real source import (a dependency
 * with no caller is itself a finding).
 *
 * Cross-platform: source discovery uses Node's fs APIs (recursive readdir), not
 * a shell `grep`/`execSync`, so it runs identically on Linux, macOS, and Windows.
 */
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';

const root = join(__dirname, '..', '..');
const srcDir = join(root, 'src');
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));

// SDK clients with no frontend caller — must not be dependencies.
const FORBIDDEN_SDKS = [
  '@aws-sdk/client-bedrock-runtime',
  '@aws-sdk/client-bedrock-agentcore',
  '@aws-sdk/client-cloudwatch',
  '@aws-sdk/client-cost-explorer',
  '@aws-sdk/client-eks',
  '@aws-sdk/client-gamelift',
];

// SDKs with a proven caller — must remain declared AND imported in source.
const REQUIRED_SDKS = [
  '@aws-sdk/client-sts',
  '@aws-sdk/client-cognito-identity-provider',
  'aws-jwt-verify',
];

const SOURCE_EXTENSIONS = ['.ts', '.tsx'];

/**
 * Recursively collect the text of every .ts/.tsx source file under `dir`,
 * excluding the `__tests__` tree — this guard file names the modules as string
 * literals and mocks legitimately reference them, so tests are not "callers".
 */
function readSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === '__tests__' || entry.name === 'node_modules') {
      continue;
    }
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...readSourceFiles(full));
    } else if (SOURCE_EXTENSIONS.some((ext) => entry.name.endsWith(ext))) {
      out.push(readFileSync(full, 'utf8'));
    }
  }
  return out;
}

// Read source once; every case reuses it.
const sourceTexts = readSourceFiles(srcDir);

/**
 * True if any non-test source file has a real ES import or CommonJS require of
 * the module. Matching `from '<mod>'` / `require('<mod>')` (with optional
 * subpath) means a bare mention in a comment does not count as a caller.
 */
function importedSomewhere(moduleName: string): boolean {
  const escaped = moduleName.replace(/[/\\^$*+?.()|[\]{}]/g, '\\$&');
  const pattern = new RegExp(`(from|require\\()\\s*['"]${escaped}(/[^'"]*)?['"]`);
  return sourceTexts.some((text) => pattern.test(text));
}

describe('frontend AWS SDK least privilege (#458)', () => {
  it.each(FORBIDDEN_SDKS)('%s is NOT a dependency (no frontend caller)', (name) => {
    expect(pkg.dependencies?.[name]).toBeUndefined();
    expect(pkg.devDependencies?.[name]).toBeUndefined();
  });

  it.each(FORBIDDEN_SDKS)('%s is not imported anywhere in src/', (name) => {
    expect(importedSomewhere(name)).toBe(false);
  });

  it.each(REQUIRED_SDKS)('%s remains declared (has a proven caller)', (name) => {
    expect(pkg.dependencies?.[name]).toBeDefined();
  });

  it.each(REQUIRED_SDKS)('%s is actually imported in src/ (declared deps have a caller)', (name) => {
    expect(importedSomewhere(name)).toBe(true);
  });
});
