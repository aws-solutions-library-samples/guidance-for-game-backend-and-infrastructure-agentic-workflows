#!/usr/bin/env node
/**
 * Bundle-size budget for the parallel-shell migration.
 *
 * Measures the summed gzip (zlib level 6) size, in KiB (1,024 bytes), of the
 * files listed under `pages[route] ∪ pages['/_app']` in
 * `.next/build-manifest.json`, split into JavaScript first-load and CSS
 * first-load. It then enforces two rules for every current-UI page (any
 * prerendered page other than the Cloudscape shell entry, including `/_error`,
 * which serves the current UI's 404 and 500 pages):
 *
 *   1. No Cloudscape module (`awsui` / `cloudscape-design`) and no Cloudscape
 *      global CSS appears in the page's first-load set.
 *   2. The page's JavaScript first-load does not grow more than 2 KiB gzip over
 *      its recorded baseline.
 *
 * It also reports the gzipped CSS first-load per shell so the CSS budget is
 * tracked alongside the JavaScript budget.
 *
 * The gate also fails when the manifest lists no current-UI page to measure
 * (for example a skipped or broken build whose manifest holds only `/_app`), or
 * when a baselined current-UI page is absent from the manifest, so a build that
 * produced nothing to protect can never pass the budget vacuously.
 *
 * Run `node scripts/measure-bundles.mjs` from `ui/` after `next build`. Pass
 * `--json` for machine-readable output. `BUNDLE_NEXT_DIR` and
 * `BUNDLE_BASELINE_PATH` override the `.next` and baseline locations so the
 * budget logic can be tested against a synthetic build. The process exits
 * non-zero if any rule fails, so it can gate CI.
 *
 * Gzip sizes depend on the zlib build in the Node running this script, so the
 * baselines are recorded in the CI e2e image and a local run on another Node
 * can report a different growth. See the `description` in
 * `bundle-baseline.json` before re-recording.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { gzipSync } from 'node:zlib';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const UI_DIR = dirname(SCRIPT_DIR);
// `.next` and the baseline default to the real build locations but can be
// pointed elsewhere so the budget logic is testable against a synthetic build
// without running a full `next build`.
const NEXT_DIR = process.env.BUNDLE_NEXT_DIR ?? join(UI_DIR, '.next');
const BASELINE_PATH = process.env.BUNDLE_BASELINE_PATH ?? join(SCRIPT_DIR, 'bundle-baseline.json');

const KIB = 1024;
const GROWTH_CEILING_KIB = 2;
const CLOUDSCAPE_PATTERN = /awsui|cloudscape-design/;
/** Pages that are expected to carry Cloudscape and are not current-UI pages. */
const CLOUDSCAPE_PAGES = new Set(['/shell-cloudscape']);
/** Manifest keys that are not real pages. `/_error` IS scanned: it serves the
 *  current UI's 404 and 500 pages, so a Cloudscape import into it must fail the
 *  budget like any other current-UI page. */
const NON_PAGE_KEYS = new Set(['/_app']);

function gzipSizeOf(relPath) {
  return gzipSync(readFileSync(join(NEXT_DIR, relPath)), { level: 6 }).length;
}

function unionFiles(manifest, route) {
  const app = manifest.pages['/_app'] ?? [];
  const page = manifest.pages[route] ?? [];
  return Array.from(new Set([...page, ...app]));
}

function measure(manifest, route) {
  const files = unionFiles(manifest, route);
  const js = files.filter((f) => f.endsWith('.js'));
  const css = files.filter((f) => f.endsWith('.css'));
  const jsBytes = js.reduce((total, f) => total + gzipSizeOf(f), 0);
  const cssBytes = css.reduce((total, f) => total + gzipSizeOf(f), 0);
  const cloudscapeFiles = files.filter(
    (f) => (f.endsWith('.js') || f.endsWith('.css')) && CLOUDSCAPE_PATTERN.test(readFileSync(join(NEXT_DIR, f), 'utf8')),
  );
  return { route, jsBytes, cssBytes, cloudscapeFiles };
}

function toKiB(bytes) {
  return bytes / KIB;
}

function main() {
  const asJson = process.argv.includes('--json');
  const manifest = JSON.parse(readFileSync(join(NEXT_DIR, 'build-manifest.json'), 'utf8'));
  const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'));

  const routes = Object.keys(manifest.pages).filter((p) => !NON_PAGE_KEYS.has(p));
  const results = routes.map((route) => measure(manifest, route));

  const failures = [];
  const report = {};

  // A build that produced no current-UI page (for example a skipped or broken
  // `next build` whose manifest holds only `/_app`) must fail rather than
  // silently report the budget OK: there is nothing to protect and the gate
  // would pass vacuously.
  if (routes.length === 0) {
    failures.push('manifest lists no current-UI page to measure (is `.next` from a complete build?)');
  }

  // Every baselined current-UI page must appear in the manifest. A baselined
  // page absent from the manifest means the build did not emit it, so its
  // growth ceiling would go unenforced; fail instead of skipping it silently.
  const baselinePages = baseline.pages ?? {};
  for (const baselinedRoute of Object.keys(baselinePages)) {
    if (!routes.includes(baselinedRoute)) {
      failures.push(`${baselinedRoute}: baselined current-UI page is missing from the build manifest`);
    }
  }

  for (const result of results) {
    const { route, jsBytes, cssBytes, cloudscapeFiles } = result;
    const isCloudscape = CLOUDSCAPE_PAGES.has(route);
    const baselineKiB = baseline.pages?.[route]?.jsFirstLoadKiB;
    const jsKiB = toKiB(jsBytes);
    const cssKiB = toKiB(cssBytes);

    report[route] = {
      jsFirstLoadKiB: Number(jsKiB.toFixed(1)),
      cssFirstLoadKiB: Number(cssKiB.toFixed(1)),
      shell: isCloudscape ? 'cloudscape' : 'current',
      cloudscapeFiles,
    };

    if (!isCloudscape) {
      if (cloudscapeFiles.length > 0) {
        failures.push(`${route}: current-UI page carries Cloudscape code/CSS: ${cloudscapeFiles.join(', ')}`);
      }
      if (typeof baselineKiB === 'number') {
        const growth = jsKiB - baselineKiB;
        report[route].growthKiB = Number(growth.toFixed(2));
        if (growth > GROWTH_CEILING_KIB) {
          failures.push(
            `${route}: first-load grew ${growth.toFixed(2)} KiB (baseline ${baselineKiB} KiB, ceiling ${GROWTH_CEILING_KIB} KiB)`,
          );
        }
      }
    }
  }

  if (asJson) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  } else {
    for (const [route, data] of Object.entries(report)) {
      const growth = data.growthKiB === undefined ? '' : ` (growth ${data.growthKiB >= 0 ? '+' : ''}${data.growthKiB} KiB)`;
      const flag = data.cloudscapeFiles.length > 0 ? ' [cloudscape]' : '';
      process.stdout.write(
        `${route.padEnd(20)} shell=${data.shell.padEnd(10)} JS=${data.jsFirstLoadKiB}KiB CSS=${data.cssFirstLoadKiB}KiB${growth}${flag}\n`,
      );
    }
  }

  if (failures.length > 0) {
    process.stderr.write(`\nBundle budget FAILED:\n${failures.map((f) => `  - ${f}`).join('\n')}\n`);
    process.exit(1);
  }

  process.stderr.write('\nBundle budget OK\n');
}

main();
