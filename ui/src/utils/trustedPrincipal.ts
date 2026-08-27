export interface TrustedPrincipal {
  readonly user_id: string;
  readonly client_id: string;
  readonly audience: string;
  readonly groups: readonly string[];
  readonly scopes: readonly string[];
  readonly tenant: string;
  readonly workspace: string;
}

export interface TrustedIdentityBinding {
  readonly clientId: string | undefined;
  readonly audience?: string | undefined;
  readonly tenant: string | undefined;
  readonly workspace: string | undefined;
}

export class TrustedPrincipalClaimsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TrustedPrincipalClaimsError';
  }
}

export class TrustedPrincipalConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TrustedPrincipalConfigError';
  }
}

function requiredConfigValue(name: string, value: string | undefined): string {
  const normalized = value?.trim();
  if (!normalized) {
    throw new TrustedPrincipalConfigError(`${name} is not configured`);
  }
  return normalized;
}

function requiredClaim(claims: Record<string, unknown>, name: string): string {
  const value = claims[name];
  if (typeof value !== 'string' || !value || value !== value.trim()) {
    throw new TrustedPrincipalClaimsError(`${name} claim is invalid`);
  }
  return value;
}

function stringArrayClaim(claims: Record<string, unknown>, name: string): readonly string[] {
  const value = claims[name];
  if (value === undefined) {
    return Object.freeze([]);
  }
  if (
    !Array.isArray(value) ||
    value.some(item => typeof item !== 'string' || !item || item !== item.trim())
  ) {
    throw new TrustedPrincipalClaimsError(`${name} claim is invalid`);
  }
  return Object.freeze([...new Set(value)]);
}

function scopesClaim(claims: Record<string, unknown>): readonly string[] {
  const value = claims.scope;
  if (value === undefined) {
    return Object.freeze([]);
  }
  if (typeof value !== 'string') {
    throw new TrustedPrincipalClaimsError('scope claim is invalid');
  }
  const scopes = value.trim() ? value.trim().split(/\s+/) : [];
  return Object.freeze([...new Set(scopes)]);
}

export function buildTrustedPrincipal(
  claims: Record<string, unknown>,
  binding: TrustedIdentityBinding,
): TrustedPrincipal {
  const expectedClientId = requiredConfigValue('COGNITO_CLIENT_ID', binding.clientId);
  const expectedAudience = requiredConfigValue(
    'GBAW_COGNITO_AUDIENCE',
    binding.audience || expectedClientId,
  );
  const tenant = requiredConfigValue('GBAW_TENANT_ID', binding.tenant);
  const workspace = requiredConfigValue('GBAW_WORKSPACE_ID', binding.workspace);

  if (claims.token_use !== 'access') {
    throw new TrustedPrincipalClaimsError('token_use claim is invalid');
  }

  const userId = requiredClaim(claims, 'sub');
  const clientId = requiredClaim(claims, 'client_id');
  if (clientId !== expectedClientId) {
    throw new TrustedPrincipalClaimsError('client_id claim does not match');
  }

  const audience = claims.aud === undefined ? clientId : requiredClaim(claims, 'aud');
  if (audience !== expectedAudience) {
    throw new TrustedPrincipalClaimsError('audience claim does not match');
  }

  return Object.freeze({
    user_id: userId,
    client_id: clientId,
    audience,
    groups: stringArrayClaim(claims, 'cognito:groups'),
    scopes: scopesClaim(claims),
    tenant,
    workspace,
  });
}
