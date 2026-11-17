/**
 * Offline checks for `ui/scripts/get-test-access-token.mjs` (issue #784).
 *
 * The helper runs as a child process against a fake Cognito bound to
 * 127.0.0.1 on an ephemeral port, through its loopback-only endpoint seam. The
 * fake answers the SRP flow the real `amazon-cognito-identity-js` client sends
 * (InitiateAuth with USER_SRP_AUTH, then RespondToAuthChallenge with
 * PASSWORD_VERIFIER) without verifying the proof, so these tests cover the
 * client flow and output hygiene, not Cognito's verification.
 *
 * Every identifier, password, and token here is synthetic.
 */
import { spawn } from 'child_process';
import { createServer, IncomingMessage, Server, ServerResponse } from 'http';
import { AddressInfo } from 'net';
import { join } from 'path';

const HELPER = join(__dirname, '..', '..', '..', 'scripts', 'get-test-access-token.mjs');
const USERNAME = 'synthetic-test-user';
const PASSWORD = 'Synthetic-Passw0rd!';

function fakeJwt(tokenUse: string): string {
  const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString('base64url');
  const now = Math.floor(Date.now() / 1000);
  return [
    encode({ alg: 'RS256', kid: 'synthetic' }),
    encode({ sub: '00000000-0000-4000-8000-000000000000', token_use: tokenUse, iat: now, exp: now + 3600 }),
    'c3ludGhldGljLXNpZ25hdHVyZQ',
  ].join('.');
}

const ACCESS_TOKEN = fakeJwt('access');
const ID_TOKEN = fakeJwt('id');
const REFRESH_TOKEN = 'synthetic-refresh-token-must-never-print';

type Scenario = 'success' | 'new-password' | 'not-authorized';

interface FakeCognito {
  endpoint: string;
  requests: { target: string; body: string }[];
  close: () => Promise<void>;
}

async function startFakeCognito(scenario: Scenario): Promise<FakeCognito> {
  const requests: { target: string; body: string }[] = [];
  const reply = (res: ServerResponse, status: number, payload: object, errorType?: string) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/x-amz-json-1.1' };
    if (errorType) headers['x-amzn-ErrorType'] = errorType;
    res.writeHead(status, headers);
    res.end(JSON.stringify(payload));
  };
  const server: Server = createServer((req: IncomingMessage, res: ServerResponse) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      const target = String(req.headers['x-amz-target'] ?? '');
      requests.push({ target, body });
      const request = JSON.parse(body || '{}');
      if (target.endsWith('.InitiateAuth')) {
        if (scenario === 'not-authorized') {
          reply(
            res,
            400,
            { __type: 'NotAuthorizedException', message: 'Incorrect username or password.' },
            'NotAuthorizedException',
          );
          return;
        }
        reply(res, 200, {
          ChallengeName: 'PASSWORD_VERIFIER',
          ChallengeParameters: {
            SALT: 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
            SECRET_BLOCK: Buffer.from('synthetic-secret-block').toString('base64'),
            SRP_B: 'ab'.repeat(192),
            USERNAME: request.AuthParameters?.USERNAME,
            USER_ID_FOR_SRP: request.AuthParameters?.USERNAME,
          },
        });
        return;
      }
      if (target.endsWith('.RespondToAuthChallenge')) {
        if (scenario === 'new-password') {
          reply(res, 200, {
            ChallengeName: 'NEW_PASSWORD_REQUIRED',
            Session: 'synthetic-session',
            ChallengeParameters: { userAttributes: '{}', requiredAttributes: '[]' },
          });
          return;
        }
        reply(res, 200, {
          AuthenticationResult: {
            AccessToken: ACCESS_TOKEN,
            IdToken: ID_TOKEN,
            RefreshToken: REFRESH_TOKEN,
            ExpiresIn: 3600,
            TokenType: 'Bearer',
          },
          ChallengeParameters: {},
        });
        return;
      }
      reply(res, 400, { __type: 'InvalidAction', message: 'unexpected' }, 'InvalidAction');
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  return {
    endpoint: `http://127.0.0.1:${port}/`,
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

interface Result {
  code: number | null;
  stdout: string;
  stderr: string;
}

function runHelper(args: string[], env: Record<string, string>): Promise<Result> {
  return new Promise((resolve, reject) => {
    // A minimal environment: no inherited AWS settings or test credentials.
    const child = spawn(process.execPath, [HELPER, ...args], {
      env: { PATH: process.env.PATH ?? '', ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => (stdout += chunk));
    child.stderr.on('data', (chunk) => (stderr += chunk));
    child.on('error', reject);
    child.on('close', (code) => resolve({ code, stdout, stderr }));
    child.stdin.end();
  });
}

function poolEnv(endpoint: string): Record<string, string> {
  return {
    GBAW_COGNITO_USER_POOL_ID: 'us-west-2_EXAMPLE',
    GBAW_COGNITO_CLIENT_ID: 'exampleclientid000000000000',
    GBAW_TEST_COGNITO_ENDPOINT: endpoint,
  };
}

function expectNoSecrets(result: Result) {
  for (const stream of [result.stdout, result.stderr]) {
    expect(stream).not.toContain(PASSWORD);
    expect(stream).not.toContain(REFRESH_TOKEN);
    expect(stream).not.toContain(ID_TOKEN);
  }
}

describe('get-test-access-token', () => {
  let fake: FakeCognito | undefined;

  afterEach(async () => {
    await fake?.close();
    fake = undefined;
  });

  it('signs in with SRP and prints only the access token', async () => {
    fake = await startFakeCognito('success');
    const result = await runHelper(['--username', USERNAME], {
      ...poolEnv(fake.endpoint),
      GBAW_TEST_USER_PASSWORD: PASSWORD,
    });

    expect(result.code).toBe(0);
    expect(result.stdout).toBe(`${ACCESS_TOKEN}\n`);
    expect(result.stderr).toBe('');
    expectNoSecrets(result);
    expect(fake.requests.map((request) => request.target)).toEqual([
      'AWSCognitoIdentityProviderService.InitiateAuth',
      'AWSCognitoIdentityProviderService.RespondToAuthChallenge',
    ]);
    const initiate = JSON.parse(fake.requests[0].body);
    expect(initiate.AuthFlow).toBe('USER_SRP_AUTH');
    expect(initiate.AuthParameters.SRP_A).toMatch(/^[0-9a-f]+$/);
    // SRP proves the password without sending it.
    for (const request of fake.requests) {
      expect(request.body).not.toContain(PASSWORD);
    }
  });

  it('reads the username from GBAW_TEST_USERNAME', async () => {
    fake = await startFakeCognito('success');
    const result = await runHelper([], {
      ...poolEnv(fake.endpoint),
      GBAW_TEST_USERNAME: USERNAME,
      GBAW_TEST_USER_PASSWORD: PASSWORD,
    });

    expect(result.code).toBe(0);
    expect(result.stdout).toBe(`${ACCESS_TOKEN}\n`);
    expect(JSON.parse(fake.requests[0].body).AuthParameters.USERNAME).toBe(USERNAME);
  });

  it.each([
    ['--password', PASSWORD],
    [`--password=${PASSWORD}`],
    ['--pass', PASSWORD],
  ])('refuses a password argument (%s) before any request', async (...passwordArgs: string[]) => {
    fake = await startFakeCognito('success');
    const result = await runHelper(['--username', USERNAME, ...passwordArgs], poolEnv(fake.endpoint));

    expect(result.code).toBe(2);
    expect(result.stdout).toBe('');
    expect(result.stderr).toContain('never accepted as an argument');
    expectNoSecrets(result);
    expect(fake.requests).toEqual([]);
  });

  it('does not echo an unrecognized argument, which may be a mistyped secret', async () => {
    fake = await startFakeCognito('success');
    const result = await runHelper(['--username', USERNAME, PASSWORD], {
      ...poolEnv(fake.endpoint),
      GBAW_TEST_USER_PASSWORD: PASSWORD,
    });

    expect(result.code).toBe(2);
    expect(result.stdout).toBe('');
    expectNoSecrets(result);
    expect(fake.requests).toEqual([]);
  });

  it('fails without a password when no terminal can prompt', async () => {
    fake = await startFakeCognito('success');
    const result = await runHelper(['--username', USERNAME], poolEnv(fake.endpoint));

    expect(result.code).toBe(2);
    expect(result.stdout).toBe('');
    expect(result.stderr).toContain('GBAW_TEST_USER_PASSWORD');
    expect(fake.requests).toEqual([]);
  });

  it('reports a temporary password without printing anything on stdout', async () => {
    fake = await startFakeCognito('new-password');
    const result = await runHelper(['--username', USERNAME], {
      ...poolEnv(fake.endpoint),
      GBAW_TEST_USER_PASSWORD: PASSWORD,
    });

    expect(result.code).toBe(1);
    expect(result.stdout).toBe('');
    expect(result.stderr).toContain('admin-set-user-password --permanent');
    expectNoSecrets(result);
  });

  it('reports a rejected sign-in by error code only', async () => {
    fake = await startFakeCognito('not-authorized');
    const result = await runHelper(['--username', USERNAME], {
      ...poolEnv(fake.endpoint),
      GBAW_TEST_USER_PASSWORD: PASSWORD,
    });

    expect(result.code).toBe(1);
    expect(result.stdout).toBe('');
    expect(result.stderr).toBe('get-test-access-token: Sign-in failed (NotAuthorizedException).\n');
    expectNoSecrets(result);
  });

  it.each(['https://cognito-idp.us-west-2.amazonaws.com/', 'http://localhost:9/', 'http://127.0.0.1:9/extra'])(
    'accepts only a loopback test endpoint, not %s',
    async (endpoint) => {
      const result = await runHelper(['--username', USERNAME], {
        ...poolEnv(endpoint),
        GBAW_TEST_USER_PASSWORD: PASSWORD,
      });

      expect(result.code).toBe(2);
      expect(result.stdout).toBe('');
      expect(result.stderr).toContain('accepts only http://127.0.0.1');
    },
  );
});
