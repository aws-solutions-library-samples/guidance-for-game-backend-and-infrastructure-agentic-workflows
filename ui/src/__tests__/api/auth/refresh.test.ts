import { createMocks } from 'node-mocks-http';

const mockSend = jest.fn();
const mockAccessVerify = jest.fn();
const mockIdVerify = jest.fn();
const mockInitiateAuthCommand = jest.fn((input) => ({ input }));

jest.mock('@aws-sdk/client-cognito-identity-provider', () => ({
  CognitoIdentityProviderClient: jest.fn(() => ({ send: mockSend })),
  InitiateAuthCommand: function InitiateAuthCommand(input: unknown) {
    return mockInitiateAuthCommand(input);
  },
}));

jest.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: jest.fn((options: { tokenUse: string }) => ({
      verify: options.tokenUse === 'access' ? mockAccessVerify : mockIdVerify,
    })),
  },
}));

jest.mock('@/utils/logger', () => ({ logError: jest.fn() }));

import handler from '@/pages/api/auth/refresh';

describe('/api/auth/refresh', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NODE_ENV = 'production';
    process.env.AWS_REGION = 'us-west-2';
    process.env.COGNITO_USER_POOL_ID = 'pool-1';
    process.env.COGNITO_CLIENT_ID = 'client-1';
    process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS = '8';
    const now = Math.floor(Date.now() / 1000);
    mockSend.mockResolvedValue({
      AuthenticationResult: { AccessToken: 'new-access', IdToken: 'new-id', ExpiresIn: 3600 },
    });
    mockAccessVerify.mockResolvedValue({ sub: 'user-1', exp: now + 3600 });
    mockIdVerify.mockResolvedValue({ sub: 'user-1', exp: now + 3600 });
  });

  afterEach(() => {
    delete process.env.AWS_REGION;
    delete process.env.COGNITO_USER_POOL_ID;
    delete process.env.COGNITO_CLIENT_ID;
    delete process.env.GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS;
  });

  function request(cookie: string, origin = 'https://app.example.com') {
    return createMocks({
      method: 'POST',
      headers: { host: 'app.example.com', origin, cookie },
    });
  }

  function validCookie() {
    const deadline = Math.floor(Date.now() / 1000) + 1800;
    return `cognito_refresh_token=refresh; cognito_session_deadline=${deadline}`;
  }

  it('exchanges the HttpOnly refresh token and rotates only access and ID cookies', async () => {
    const { req, res } = request(validCookie());

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    expect(mockInitiateAuthCommand).toHaveBeenCalledWith({
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: 'client-1',
      AuthParameters: { REFRESH_TOKEN: 'refresh' },
    });
    expect(mockAccessVerify).toHaveBeenCalledWith('new-access');
    expect(mockIdVerify).toHaveBeenCalledWith('new-id');
    const cookies = res.getHeader('Set-Cookie') as string[];
    expect(cookies).toHaveLength(2);
    expect(cookies.join(';')).toContain('cognito_access_token=new-access');
    expect(cookies.join(';')).toContain('cognito_id_token=new-id');
    expect(cookies.join(';')).not.toContain('cognito_refresh_token');
    expect(cookies.join(';')).not.toContain('cognito_session_deadline');
  });

  it('fails closed and clears cookies when the absolute deadline is missing', async () => {
    const { req, res } = request('cognito_refresh_token=refresh');

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(mockSend).not.toHaveBeenCalled();
    expect(res.getHeader('Set-Cookie')).toHaveLength(4);
  });

  it('fails closed and clears cookies after absolute expiry', async () => {
    const deadline = Math.floor(Date.now() / 1000) - 1;
    const { req, res } = request(`cognito_refresh_token=refresh; cognito_session_deadline=${deadline}`);

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('clears the session when Cognito rejects the refresh token', async () => {
    mockSend.mockRejectedValue(new Error('NotAuthorizedException'));
    const { req, res } = request(validCookie());

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
    expect(res.getHeader('Set-Cookie')).toHaveLength(4);
  });

  it('rejects refreshed tokens for different subjects', async () => {
    mockIdVerify.mockResolvedValue({ sub: 'user-2', exp: Math.floor(Date.now() / 1000) + 3600 });
    const { req, res } = request(validCookie());

    await handler(req, res);

    expect(res._getStatusCode()).toBe(401);
  });

  it('blocks cross-origin refresh requests', async () => {
    const { req, res } = request(validCookie(), 'https://evil.example.com');
    await handler(req, res);
    expect(res._getStatusCode()).toBe(403);
    expect(mockSend).not.toHaveBeenCalled();
  });
});
