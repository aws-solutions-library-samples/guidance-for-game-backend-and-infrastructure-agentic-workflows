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
 * Three checks keep that true:
 *   1. Source modules — none of the account-management modules exist.
 *   2. Source tree — `src/pages` and `src/pages/api` contain no `admin` entry,
 *      and no source file links to an `/admin/*` route. The Pages Router maps
 *      files on disk directly to routes and `next.config.mjs` adds no rewrites,
 *      so an absent `admin` directory means an absent route. This runs on every
 *      unit run, with no build required.
 *   3. Production build — the `.next` route manifests, when present, expose no
 *      `/admin/*` page or API. This leg is a build-time backstop. Plain unit
 *      runs have no `.next`, so it is skipped there; set
 *      REQUIRE_NEXT_BUILD_MANIFEST=true (the e2e job does, after the build) to
 *      require the page manifest to be present and to fail if it is missing.
 *
 * Cross-platform: discovery uses Node's fs APIs, not a shell `grep`, so it runs
 * identically on Linux, macOS, and Windows.
 */
import { existsSync, readFileSync, readdirSync } from 'fs';
import { join, relative } from 'path';

const uiRoot = join(__dirname, '..', '..');
const srcDir = join(uiRoot, 'src');
const nextDir = join(uiRoot, '.next');

const REMOVED_MODULES = [
  'src/pages/admin/users.tsx',
  'src/pages/api/admin/users.ts',
  'src/pages/api/admin/approve.ts',
];

const SOURCE_EXTENSIONS = ['.ts', '.tsx'];

// Pages Router directories whose entries map directly to public routes.
const ROUTE_DIRS = ['src/pages', 'src/pages/api'];

const ADMIN_ROUTE = /^\/(api\/)?admin(\/|$)/;
// A source reference to any /admin or /api/admin route in a string, template
// literal, or JSX href.
const ADMIN_LINK = /['"`]\/(api\/)?admin(\/|['"`])/;

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

/**
 * Collect every admin route from a Next.js route manifest object. Covers both
 * manifest shapes: `pages-manifest.json` keys routes by path, while
 * `routes-manifest.json` lists them as `page` string values under
 * `staticRoutes[]`/`dynamicRoutes[]`.
 */
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
        if (key === 'page' && typeof value === 'string' && ADMIN_ROUTE.test(value)) {
          keys.add(value);
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

  it.each(ROUTE_DIRS)('%s has no admin route entry', (rel) => {
    const names = readdirSync(join(uiRoot, rel)).map((n) => n.replace(/\.(t|j)sx?$/, ''));
    expect(names).not.toContain('admin');
  });

  it('no source file links to an /admin route', () => {
    const offenders = collectSourceFiles(srcDir)
      .filter((file) => !file.includes(`${join('src', '__tests__')}`))
      .filter((file) => ADMIN_LINK.test(readFileSync(file, 'utf8')))
      .map((file) => relative(uiRoot, file));
    expect(offenders).toEqual([]);
  });

  // The production build manifests only exist after `npm run build`. A plain
  // unit run (CI's frontend-tests job, or `npm test`) has no `.next`, so the
  // backstop below is skipped and the source checks above carry the guarantee.
  // Set REQUIRE_NEXT_BUILD_MANIFEST=true to require the page manifest and fail
  // if it is absent; the e2e job sets it when running this suite after the
  // build.
  const pagesManifest = join(nextDir, 'server', 'pages-manifest.json');
  const requireManifest = process.env.REQUIRE_NEXT_BUILD_MANIFEST === 'true';
  const describeBuild = requireManifest || existsSync(pagesManifest) ? describe : describe.skip;

  describeBuild('production build route manifests', () => {
    if (requireManifest) {
      it('page manifest exists (REQUIRE_NEXT_BUILD_MANIFEST=true)', () => {
        expect(existsSync(pagesManifest)).toBe(true);
      });
    }

    const manifestFiles = [
      join(nextDir, 'server', 'pages-manifest.json'),
      join(nextDir, 'server', 'middleware-manifest.json'),
      join(nextDir, 'routes-manifest.json'),
    ].filter(existsSync);

    it.each(manifestFiles.map((file) => [relative(uiRoot, file), file]))(
      '%s exposes no /admin route',
      (_label, file) => {
        const manifest = JSON.parse(readFileSync(file, 'utf8'));
        expect(adminRouteKeys(manifest)).toEqual([]);
      }
    );
  });
});
