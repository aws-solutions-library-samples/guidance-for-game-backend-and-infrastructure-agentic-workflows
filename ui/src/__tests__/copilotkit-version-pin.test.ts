/**
 * CopilotKit version-pin guard.
 *
 * WHY THIS EXISTS: a Dependabot "npm-minor-patch group" PR silently bumped
 * @copilotkit/* from 1.10.x to 1.61.0 in package-lock.json (the caret `^1.10.6`
 * permitted it; package.json never visibly changed). CopilotKit changed its
 * client↔runtime wire protocol between 1.10 and 1.50+ (GraphQL → AG-UI/SSE), so
 * our custom GraphQL proxy (pages/api/copilot/chat.ts) silently swallowed every
 * chat message. Unit tests passed because they posted a hand-crafted GraphQL
 * body, not what the real client sends.
 *
 * This test fails CI if @copilotkit ever resolves outside the 1.10.x line that
 * our proxy's GraphQL contract is built for. Bumping past it is a deliberate
 * protocol migration (rewire chat.ts to AG-UI/SSE), not a routine dep bump —
 * so it MUST break this test and force that decision.
 */
import { readFileSync } from 'fs';
import { join } from 'path';

const PINNED = ['@copilotkit/react-core', '@copilotkit/react-ui', '@copilotkit/shared'];
// The GraphQL-protocol line. Update ONLY alongside a chat.ts protocol migration.
const ALLOWED_MAJOR_MINOR = /^1\.10\./;

describe('CopilotKit version pin (protocol guard)', () => {
  const root = join(__dirname, '..', '..');
  const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));

  it.each(PINNED)('%s is pinned to an exact version (no caret/range) in package.json', (name) => {
    const spec = pkg.dependencies[name];
    expect(spec).toBeDefined();
    // Exact pin only — a leading ^ or ~ is what let the silent jump happen.
    expect(spec).toMatch(/^\d+\.\d+\.\d+$/);
    expect(spec).toMatch(ALLOWED_MAJOR_MINOR);
  });

  it.each(PINNED)('%s resolves to the 1.10.x protocol line in package-lock.json', (name) => {
    const entry = lock.packages?.[`node_modules/${name}`];
    expect(entry).toBeDefined();
    expect(entry.version).toMatch(ALLOWED_MAJOR_MINOR);
  });

  it('runtime-client-gql (the GraphQL transport our proxy speaks) is also on 1.10.x', () => {
    // Find every resolved copy; none may be on the AG-UI (>=1.50) line.
    const offenders = Object.entries(lock.packages as Record<string, { version?: string }>)
      .filter(([k]) => /node_modules\/@copilotkit\/runtime-client-gql$/.test(k))
      .map(([, v]) => v.version)
      .filter((v) => v && !ALLOWED_MAJOR_MINOR.test(v));
    expect(offenders).toEqual([]);
  });
});
