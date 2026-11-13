#!/usr/bin/env node
/**
 * Print a short-lived Cognito access token for a provisioned test user.
 *
 * Deployed validation (`GBAW_TEST_ACCESS_TOKEN` for `./test-full.sh`,
 * `./test-ai-evals.sh`, and the hosted integration tests) needs an access token
 * from the deployment's user pool. The app client allows only SRP and refresh
 * sign-in, so this helper signs in with SRP, the flow the UI uses, through
 * `amazon-cognito-identity-js`. SRP proves the password without sending it.
 *
 * Run from `ui/` after `npm ci`:
 *
 *   export GBAW_TEST_ACCESS_TOKEN="$(node scripts/get-test-access-token.mjs --username <test-user>)"
 *
 * Inputs:
 *   --username <name>       or GBAW_TEST_USERNAME. The test user's Cognito username.
 *   --project-name <name>   or PROJECT_NAME (default game-agent). Selects the
 *                           `<project>-infrastructure` stack.
 *   --region <region>       or AWS_REGION / AWS_DEFAULT_REGION (default us-west-2).
 *   Password: GBAW_TEST_USER_PASSWORD, or a hidden prompt on a terminal.
 *   It is never accepted as an argument, where shell history and process
 *   listings would record it.
 *
 * The user pool and app client IDs come from the stack's `UserPoolId` and
 * `UserPoolClientId` outputs through the AWS CLI (honoring AWS_PROFILE), unless
 * GBAW_COGNITO_USER_POOL_ID and GBAW_COGNITO_CLIENT_ID are both set.
 *
 * Output: stdout carries only the access token and a newline. The password,
 * ID token, and refresh token are never printed. Errors go to stderr as fixed
 * messages plus the Cognito error code. Exit 2 is a usage error; exit 1 is a
 * lookup or sign-in failure.
 *
 * GBAW_TEST_COGNITO_ENDPOINT is a test seam for the offline fake Cognito. Only
 * a loopback `http://127.0.0.1:<port>/` URL is accepted.
 */
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import process from 'node:process';
import { parseArgs } from 'node:util';

const require = createRequire(import.meta.url);

const DEFAULT_PROJECT_NAME = 'game-agent';
const DEFAULT_REGION = 'us-west-2';
const USER_POOL_ID_PATTERN = /^[a-z]{2}(-[a-z]+)+-\d_[0-9A-Za-z]+$/;
const CLIENT_ID_PATTERN = /^[0-9A-Za-z]{1,128}$/;
const PROJECT_NAME_PATTERN = /^[A-Za-z0-9-]{1,128}$/;
const REGION_PATTERN = /^[a-z]{2}(-[a-z]+)+-\d$/;
const LOOPBACK_ENDPOINT_PATTERN = /^http:\/\/127\.0\.0\.1:\d{1,5}\/$/;

class HelperError extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.exitCode = exitCode;
  }
}

function usageError(message) {
  return new HelperError(`${message}\nUsage: node scripts/get-test-access-token.mjs --username <test-user>`, 2);
}

function parseOptions(argv) {
  // Refuse a password argument before parsing, so no parser message can echo it.
  if (argv.some((argument) => /^--?pass(word)?(=|$)/i.test(argument))) {
    throw usageError(
      'The password is never accepted as an argument. Set GBAW_TEST_USER_PASSWORD or enter it at the prompt.',
    );
  }
  try {
    const { values } = parseArgs({
      args: argv,
      allowPositionals: false,
      strict: true,
      options: {
        username: { type: 'string' },
        'project-name': { type: 'string' },
        region: { type: 'string' },
        help: { type: 'boolean', short: 'h' },
      },
    });
    return values;
  } catch {
    // The parser's message quotes the offending argument; keep it out of output.
    throw usageError('Unrecognized or incomplete arguments.');
  }
}

function resolveSetting(value, envName, fallback) {
  const resolved = (value ?? process.env[envName] ?? '').trim();
  return resolved || fallback;
}

function stackOutput(stackName, region, outputKey) {
  let output;
  try {
    output = execFileSync(
      'aws',
      [
        'cloudformation',
        'describe-stacks',
        '--stack-name',
        stackName,
        '--region',
        region,
        '--query',
        `Stacks[0].Outputs[?OutputKey==\`${outputKey}\`].OutputValue`,
        '--output',
        'text',
      ],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    );
  } catch {
    throw new HelperError(
      `Could not read ${outputKey} from stack ${stackName} in ${region}. Check AWS credentials, AWS_PROFILE, and --project-name.`,
    );
  }
  return output.trim();
}

function resolvePool(projectName, region) {
  let userPoolId = (process.env.GBAW_COGNITO_USER_POOL_ID ?? '').trim();
  let clientId = (process.env.GBAW_COGNITO_CLIENT_ID ?? '').trim();
  if (!userPoolId || !clientId) {
    const stackName = `${projectName}-infrastructure`;
    userPoolId = stackOutput(stackName, region, 'UserPoolId');
    clientId = stackOutput(stackName, region, 'UserPoolClientId');
  }
  if (!USER_POOL_ID_PATTERN.test(userPoolId)) {
    throw new HelperError('The user pool ID is missing or malformed.');
  }
  if (!CLIENT_ID_PATTERN.test(clientId)) {
    throw new HelperError('The app client ID is missing or malformed.');
  }
  return { userPoolId, clientId };
}

function resolveEndpoint() {
  const endpoint = (process.env.GBAW_TEST_COGNITO_ENDPOINT ?? '').trim();
  if (!endpoint) return undefined;
  if (!LOOPBACK_ENDPOINT_PATTERN.test(endpoint)) {
    throw usageError('GBAW_TEST_COGNITO_ENDPOINT is a test seam and accepts only http://127.0.0.1:<port>/.');
  }
  return endpoint;
}

function promptHidden(question) {
  return new Promise((resolve, reject) => {
    const input = process.stdin;
    let value = '';
    const cleanup = () => {
      input.off('data', onData);
      input.setRawMode(false);
      input.pause();
      process.stderr.write('\n');
    };
    const onData = (chunk) => {
      for (const character of chunk) {
        if (character === '\r' || character === '\n') {
          cleanup();
          resolve(value);
          return;
        }
        if (character === '\u0003' || character === '\u0004') {
          cleanup();
          reject(new HelperError('Cancelled.', 2));
          return;
        }
        if (character === '\u007f' || character === '\b') {
          value = value.slice(0, -1);
        } else {
          value += character;
        }
      }
    };
    process.stderr.write(question);
    input.setEncoding('utf8');
    input.setRawMode(true);
    input.resume();
    input.on('data', onData);
  });
}

async function readPassword() {
  const fromEnv = process.env.GBAW_TEST_USER_PASSWORD;
  // Drop it from this process's environment so nothing spawned later inherits it.
  delete process.env.GBAW_TEST_USER_PASSWORD;
  if (fromEnv) return fromEnv;
  if (!process.stdin.isTTY) {
    throw usageError('No password: set GBAW_TEST_USER_PASSWORD, or run in a terminal to be prompted.');
  }
  const password = await promptHidden('Test user password: ');
  if (!password) throw usageError('No password entered.');
  return password;
}

function signIn({ userPoolId, clientId, endpoint, username, password }) {
  const { AuthenticationDetails, CognitoUser, CognitoUserPool } = require('amazon-cognito-identity-js');
  const pool = new CognitoUserPool({ UserPoolId: userPoolId, ClientId: clientId, endpoint });
  const user = new CognitoUser({ Username: username, Pool: pool });
  const details = new AuthenticationDetails({ Username: username, Password: password });

  return new Promise((resolve, reject) => {
    const unsupported = (challenge) => () =>
      reject(new HelperError(`Sign-in needs a ${challenge} challenge, which this helper does not answer.`));
    user.authenticateUser(details, {
      onSuccess: (session) => resolve(session.getAccessToken().getJwtToken()),
      onFailure: (error) => {
        const code = typeof error?.code === 'string' && /^[A-Za-z]+$/.test(error.code) ? error.code : 'UnknownError';
        reject(new HelperError(`Sign-in failed (${code}).`));
      },
      newPasswordRequired: () =>
        reject(
          new HelperError(
            'The test user still has a temporary password. Set a permanent one with ' +
              '`aws cognito-idp admin-set-user-password --permanent`, then retry.',
          ),
        ),
      mfaRequired: unsupported('SMS MFA'),
      totpRequired: unsupported('TOTP MFA'),
      selectMFAType: unsupported('MFA selection'),
      mfaSetup: unsupported('MFA setup'),
      customChallenge: unsupported('custom'),
    });
  });
}

async function main(argv) {
  const options = parseOptions(argv);
  if (options.help) {
    process.stderr.write('Usage: node scripts/get-test-access-token.mjs --username <test-user>\n');
    return;
  }
  const username = resolveSetting(options.username, 'GBAW_TEST_USERNAME', '');
  if (!username) throw usageError('No username: pass --username or set GBAW_TEST_USERNAME.');
  const projectName = resolveSetting(options['project-name'], 'PROJECT_NAME', DEFAULT_PROJECT_NAME);
  if (!PROJECT_NAME_PATTERN.test(projectName)) throw usageError('The project name is malformed.');
  const region = resolveSetting(
    options.region,
    'AWS_REGION',
    resolveSetting(undefined, 'AWS_DEFAULT_REGION', DEFAULT_REGION),
  );
  if (!REGION_PATTERN.test(region)) throw usageError('The region is malformed.');

  const endpoint = resolveEndpoint();
  const password = await readPassword();
  const { userPoolId, clientId } = resolvePool(projectName, region);
  const accessToken = await signIn({ userPoolId, clientId, endpoint, username, password });
  process.stdout.write(`${accessToken}\n`);
}

main(process.argv.slice(2)).catch((error) => {
  const message = error instanceof HelperError ? error.message : 'Unexpected failure.';
  process.stderr.write(`get-test-access-token: ${message}\n`);
  process.exitCode = error instanceof HelperError ? error.exitCode : 1;
});
