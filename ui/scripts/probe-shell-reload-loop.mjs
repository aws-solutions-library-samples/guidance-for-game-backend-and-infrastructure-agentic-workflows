#!/usr/bin/env node
/**
 * Manual probe for the shell-boundary reload bound.
 *
 * The shell-boundary reload only misbehaves on a production server: Next's
 * `next dev` renders an empty error component on a client exception, while
 * `next start` re-renders `_app` with the real `/_error` page, which declares no
 * shell. Inside a Cloudscape document that looks like a permanent shell
 * mismatch, so an unbounded reload would loop forever. A failed `/api/config`
 * request is enough to trigger a client exception, because the fail-closed auth
 * path then builds a Cognito pool with empty IDs and throws.
 *
 * This probe reproduces that exact failure against a real production build and
 * measures it. It is a script rather than a committed Playwright spec because
 * reproducing the loop needs a `next build` + `next start` server (the dev
 * server cannot show it) and the committed Cloudscape Playwright suite runs
 * against `next dev`; wiring a second production server into CI is out of scope
 * for this change. Run it by hand after `npm run build`:
 *
 *   node scripts/probe-shell-reload-loop.mjs
 *
 * It starts `next start -H 127.0.0.1` with `GBAW_UI_SHELL=cloudscape`, aborts
 * every `/api/config` request, loads `/`, counts the full document loads of `/`
 * over a fixed window, and exits non-zero if more than one occurs. With the
 * reload bounded, the expected count is 1 (the initial load). An unbounded
 * reload produces dozens to hundreds in the same window.
 *
 * Flags:
 *   --shell <current|cloudscape>  shell selector (default cloudscape)
 *   --port <n>                    server port (default 3410)
 *   --duration-ms <n>             measurement window after first load (default 5000)
 *   --no-abort                    do not abort /api/config (control run)
 *   --max <n>                     maximum allowed document loads (default 1)
 */

// Standard library
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

// Third-party packages
import { chromium } from '@playwright/test';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const UI_DIR = dirname(SCRIPT_DIR);
const HOST = '127.0.0.1';

function parseArgs(argv) {
  const args = { shell: 'cloudscape', port: 3410, durationMs: 5000, abort: true, max: 1 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--shell') args.shell = argv[(i += 1)];
    else if (arg === '--port') args.port = Number(argv[(i += 1)]);
    else if (arg === '--duration-ms') args.durationMs = Number(argv[(i += 1)]);
    else if (arg === '--no-abort') args.abort = false;
    else if (arg === '--max') args.max = Number(argv[(i += 1)]);
  }
  return args;
}

async function waitForServer(baseUrl, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/api/health`, { signal: AbortSignal.timeout(2000) });
      if (res.ok || res.status === 404 || res.status === 401) return;
    } catch {
      // Server not ready yet; keep polling.
    }
    await sleep(300);
  }
  throw new Error(`server did not become ready within ${timeoutMs} ms`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = `http://${HOST}:${args.port}`;

  // Start the production server bound to loopback so it is never reachable
  // beyond the local host.
  const server = spawn('npx', ['next', 'start', '--port', String(args.port), '-H', HOST], {
    cwd: UI_DIR,
    env: {
      ...process.env,
      GBAW_UI_SHELL: args.shell,
      // No skip-auth: production mode runs the real fail-closed auth path, which
      // is what turns a failed /api/config into a client exception.
      NODE_ENV: 'production',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  server.stdout.on('data', () => {});
  server.stderr.on('data', () => {});

  let browser;
  let exitCode = 0;
  try {
    await waitForServer(baseUrl, 60000);

    browser = await chromium.launch();
    const context = await browser.newContext();
    const page = await context.newPage();

    if (args.abort) {
      // Reproduce the failure: /api/config never resolves successfully.
      await page.route('**/api/config', (route) => route.abort());
    }

    // Count only full document loads of `/` on the main frame.
    let documentLoads = 0;
    page.on('response', (response) => {
      const req = response.request();
      if (req.resourceType() !== 'document' || req.frame() !== page.mainFrame()) return;
      const url = new URL(response.url());
      if (url.pathname === '/') documentLoads += 1;
    });

    await page.goto(`${baseUrl}/`, { waitUntil: 'commit' });
    // Hold the window open so an unbounded reload loop accumulates loads.
    await sleep(args.durationMs);

    const verdict = documentLoads <= args.max ? 'PASS' : 'FAIL';
    process.stdout.write(
      `shell=${args.shell} abort=${args.abort} window=${args.durationMs}ms ` +
        `documentLoadsOf/=${documentLoads} max=${args.max} => ${verdict}\n`,
    );
    if (documentLoads > args.max) exitCode = 1;
  } catch (error) {
    process.stderr.write(`probe error: ${error instanceof Error ? error.message : String(error)}\n`);
    exitCode = 2;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    // Give the server a moment to exit, then force-kill if it lingers.
    await sleep(500);
    if (!server.killed) server.kill('SIGKILL');
  }

  process.exit(exitCode);
}

main();
