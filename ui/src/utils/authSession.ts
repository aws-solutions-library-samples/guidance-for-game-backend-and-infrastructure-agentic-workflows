import type { SerializeOptions } from 'cookie';
import { serialize } from '@/utils/cookieCompat';

export const ACCESS_TOKEN_COOKIE = 'cognito_access_token';
export const ID_TOKEN_COOKIE = 'cognito_id_token';
export const REFRESH_TOKEN_COOKIE = 'cognito_refresh_token';
export const SESSION_DEADLINE_COOKIE = 'cognito_session_deadline';

export interface VerifiedTokenTiming {
  exp?: number;
  iat?: number;
  sub?: string;
}

interface InitialSessionCookies {
  accessToken: string;
  idToken: string;
  refreshToken: string;
  accessPayload: VerifiedTokenTiming;
  idPayload: VerifiedTokenTiming;
  nowSeconds?: number;
}

interface RefreshedSessionCookies {
  accessToken: string;
  idToken: string;
  accessPayload: VerifiedTokenTiming;
  idPayload: VerifiedTokenTiming;
  absoluteRemainingSeconds: number;
  nowSeconds?: number;
}

const DEFAULT_ABSOLUTE_LIFETIME_HOURS = 8;
const MIN_ABSOLUTE_LIFETIME_HOURS = 1;
const MAX_ABSOLUTE_LIFETIME_HOURS = 24;

function cookieOptions(maxAge: number): SerializeOptions {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
    maxAge,
  };
}

function boundedHours(rawValue: string | undefined): number {
  const parsed = Number(rawValue ?? DEFAULT_ABSOLUTE_LIFETIME_HOURS);
  if (!Number.isFinite(parsed)) return DEFAULT_ABSOLUTE_LIFETIME_HOURS;
  return Math.min(MAX_ABSOLUTE_LIFETIME_HOURS, Math.max(MIN_ABSOLUTE_LIFETIME_HOURS, parsed));
}

export function absoluteSessionLifetimeSeconds(): number {
  return Math.floor(boundedHours(process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS) * 60 * 60);
}

function tokenLifetimeSeconds(payload: VerifiedTokenTiming, nowSeconds: number): number {
  if (!Number.isFinite(payload.exp)) {
    throw new Error('Verified token is missing a valid expiration');
  }
  const remaining = Math.floor((payload.exp as number) - nowSeconds);
  if (remaining <= 0) {
    throw new Error('Verified token is expired');
  }
  return remaining;
}

function assertSameSubject(accessPayload: VerifiedTokenTiming, idPayload: VerifiedTokenTiming): void {
  if (!accessPayload.sub || !idPayload.sub || accessPayload.sub !== idPayload.sub) {
    throw new Error('Verified Cognito tokens do not identify the same subject');
  }
}

export function createInitialSessionCookies(input: InitialSessionCookies): string[] {
  const nowSeconds = input.nowSeconds ?? Math.floor(Date.now() / 1000);
  const absoluteSeconds = absoluteSessionLifetimeSeconds();
  assertSameSubject(input.accessPayload, input.idPayload);

  const accessMaxAge = Math.min(tokenLifetimeSeconds(input.accessPayload, nowSeconds), absoluteSeconds);
  const idMaxAge = Math.min(tokenLifetimeSeconds(input.idPayload, nowSeconds), absoluteSeconds);
  const deadline = nowSeconds + absoluteSeconds;

  return [
    serialize(ACCESS_TOKEN_COOKIE, input.accessToken, cookieOptions(accessMaxAge)),
    serialize(ID_TOKEN_COOKIE, input.idToken, cookieOptions(idMaxAge)),
    serialize(REFRESH_TOKEN_COOKIE, input.refreshToken, cookieOptions(absoluteSeconds)),
    serialize(SESSION_DEADLINE_COOKIE, String(deadline), cookieOptions(absoluteSeconds)),
  ];
}

export function createRefreshedTokenCookies(input: RefreshedSessionCookies): string[] {
  const nowSeconds = input.nowSeconds ?? Math.floor(Date.now() / 1000);
  const absoluteRemainingSeconds = Math.floor(input.absoluteRemainingSeconds);
  if (absoluteRemainingSeconds <= 0) {
    throw new Error('Absolute session lifetime has expired');
  }
  assertSameSubject(input.accessPayload, input.idPayload);

  const accessMaxAge = Math.min(
    tokenLifetimeSeconds(input.accessPayload, nowSeconds),
    absoluteRemainingSeconds,
  );
  const idMaxAge = Math.min(
    tokenLifetimeSeconds(input.idPayload, nowSeconds),
    absoluteRemainingSeconds,
  );

  return [
    serialize(ACCESS_TOKEN_COOKIE, input.accessToken, cookieOptions(accessMaxAge)),
    serialize(ID_TOKEN_COOKIE, input.idToken, cookieOptions(idMaxAge)),
  ];
}

export function absoluteRemainingSeconds(
  deadlineValue: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): number {
  if (!deadlineValue || !/^\d+$/.test(deadlineValue)) return 0;
  // The deadline cookie is defense in depth; Cognito's refresh token uses the
  // same exported lifetime and is the non-tamperable enforcement boundary.
  const configuredMaximum = absoluteSessionLifetimeSeconds();
  const remaining = Number(deadlineValue) - nowSeconds;
  if (!Number.isFinite(remaining) || remaining <= 0) return 0;
  return Math.min(Math.floor(remaining), configuredMaximum);
}

export function clearAuthCookies(): string[] {
  const expired = cookieOptions(0);
  return [
    serialize(ACCESS_TOKEN_COOKIE, '', expired),
    serialize(ID_TOKEN_COOKIE, '', expired),
    serialize(REFRESH_TOKEN_COOKIE, '', expired),
    serialize(SESSION_DEADLINE_COOKIE, '', expired),
  ];
}
