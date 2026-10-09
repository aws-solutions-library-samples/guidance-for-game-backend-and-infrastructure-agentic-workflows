/**
 * Regression guard: the sample ships administrator-provisioned access only, so
 * the bundle must contain no account-management page or API route (issue #473).
 *
 * Access is created out-of-band with `add-admin-user.sh` (or the PowerShell
 * `Add-GameAgentAdmin`), which puts a confirmed user straight into the `admin`
 * group. There is no in-app flow that lists Cognito users or approves a
 * self-signup account, and the internet-facing frontend task role is not
 * granted the Cognito administrator permissions such a flow would require.
 *
 * Two independent checks keep that true:
 *   1. Source tree — none of the account-management modules exist, and no
 *      source file links to `/admin/users`.
 *   2. Production build — when `.next` route manifests are present (CI runs
 *      `npm run build` before the Jest suite), neither the page manifest nor
 *      the serverless/edge function manifest exposes an `/admin/*` route.
 *
 * Cross-platform: discovery uses Node's fs APIs, not a shell `grep`, so it runs
 * identically on Linux, macOS, and Windows.
 */
import { existsSync, readFileSync, readdirSync } from 'fs';
import { join } from 'path';

const uiRoot = join(__dirname, '..', '..');
const srcDir = join(uiRoot, 'src');
const nextDir = join(uiRoot, '.next');

const REMOVED_MODULES = [
  'src/pages/admin/users.tsx',
  'src/pages/api/admin/users.ts',
  'src/pages/api/admin/approve.ts',
];

const SOURCE_EXTENSIONS = ['.ts', '.tsx'];

function collectSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules') {
      continue;
    }
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectSourceFiles(full));
    } else if (SOURCE_EXTENSIONS.some((ext) => entry.name.endsWith(ext))) {
      out.push(full);
    }
  }
  return out;
}

/** Collect every admin route key from a Next.js route manifest object. */
function adminRouteKeys(manifest: unknown): string[] {
  const keys = new Set<string>();
  const visit = (node: unknown) => {
    if (Array.isArray(node)) {
      node.forEach(visit);
      return;
    }
    if (node && typeof node === 'object') {
      for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
        if (key.startsWith('/admin') || key.startsWith('/api/admin')) {
          keys.add(key);
        }
        visit(value);
      }
    }
  };
  visit(manifest);
  return [...keys].sort();
}

describe('no admin user-management surface (#473)', () => {
  it.each(REMOVED_MODULES)('source module %s does not exist', (relPath) => {
    expect(existsSync(join(uiRoot, relPath))).toBe(false);
  });

  it('no source file links to the /admin/users page', () => {
    const offenders = collectSourceFiles(srcDir)
      .filter((file) => !file.includes(`${join('src', '__tests__')}`))
      .filter((file) => /['"]\/admin\/users['"]/.test(readFileSync(file, 'utf8')));
    expect(offenders).toEqual([]);
  });

  // The production build manifests only exist after `npm run build`. CI builds
  // before running Jest; when they are absent (plain unit run) the assertion is
  // skipped rather than passing vacuously under a false "no manifest" branch.
  const pagesManifest = join(nextDir, 'server', 'pages-manifest.json');
  const describeBuild = existsSync(pagesManifest) ? describe : describe.skip;

  describeBuild('production build route manifests', () => {
    const manifestFiles = [
      join(nextDir, 'server', 'pages-manifest.json'),
      join(nextDir, 'server', 'middleware-manifest.json'),
      join(nextDir, 'routes-manifest.json'),
    ].filter(existsSync);

    it.each(manifestFiles)('%s exposes no /admin route', (file) => {
      const manifest = JSON.parse(readFileSync(file, 'utf8'));
      expect(adminRouteKeys(manifest)).toEqual([]);
    });
  });
});
