import {
  buildTrustedPrincipal,
  TrustedPrincipalClaimsError,
  TrustedPrincipalConfigError,
} from '@/utils/trustedPrincipal';

const binding = {
  clientId: 'web-client',
  tenant: 'tenant-a',
  workspace: 'workspace-a',
};

const claims = {
  token_use: 'access',
  sub: 'subject-123',
  client_id: 'web-client',
  'cognito:groups': ['users', 'source-readers'],
  scope: 'openid profile',
};

describe('buildTrustedPrincipal', () => {
  it('constructs an immutable principal from access claims and trusted bindings', () => {
    const principal = buildTrustedPrincipal(claims, binding);

    expect(principal).toEqual({
      user_id: 'subject-123',
      client_id: 'web-client',
      audience: 'web-client',
      groups: ['users', 'source-readers'],
      scopes: ['openid', 'profile'],
      tenant: 'tenant-a',
      workspace: 'workspace-a',
    });
    expect(Object.isFrozen(principal)).toBe(true);
    expect(Object.isFrozen(principal.groups)).toBe(true);
    expect(Object.isFrozen(principal.scopes)).toBe(true);
  });

  it('uses and validates an explicit access-token audience', () => {
    const principal = buildTrustedPrincipal(
      { ...claims, aud: 'https://operations.example' },
      { ...binding, audience: 'https://operations.example' },
    );

    expect(principal.audience).toBe('https://operations.example');
  });

  it.each([
    ['wrong token use', { ...claims, token_use: 'id' }, binding],
    ['missing subject', { ...claims, sub: undefined }, binding],
    ['wrong client', { ...claims, client_id: 'other-client' }, binding],
    [
      'wrong audience',
      { ...claims, aud: 'https://other.example' },
      { ...binding, audience: 'https://operations.example' },
    ],
    ['malformed groups', { ...claims, 'cognito:groups': 'users' }, binding],
    ['malformed scopes', { ...claims, scope: ['openid'] }, binding],
  ])('rejects invalid claims: %s', (_label, invalidClaims, trustedBinding) => {
    expect(() => buildTrustedPrincipal(invalidClaims, trustedBinding)).toThrow(
      TrustedPrincipalClaimsError,
    );
  });

  it.each([
    ['client', { ...binding, clientId: '' }],
    ['tenant', { ...binding, tenant: undefined }],
    ['workspace', { ...binding, workspace: ' ' }],
  ])('distinguishes missing %s configuration from invalid claims', (_label, invalidBinding) => {
    expect(() => buildTrustedPrincipal(claims, invalidBinding)).toThrow(
      TrustedPrincipalConfigError,
    );
  });
});
