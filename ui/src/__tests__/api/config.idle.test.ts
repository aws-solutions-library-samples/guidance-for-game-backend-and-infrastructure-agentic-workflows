import { createMocks } from 'node-mocks-http';
import handler from '@/pages/api/config';

describe('/api/config idle-session settings', () => {
  afterEach(() => {
    delete process.env.GBAW_SESSION_IDLE_TIMEOUT_SECONDS;
    delete process.env.GBAW_SESSION_IDLE_WARNING_SECONDS;
  });

  it('returns deployment-configured idle timeout and warning threshold', () => {
    process.env.GBAW_SESSION_IDLE_TIMEOUT_SECONDS = '1200';
    process.env.GBAW_SESSION_IDLE_WARNING_SECONDS = '90';
    const { req, res } = createMocks({ method: 'GET' });

    handler(req, res);

    const session = JSON.parse(res._getData()).session;
    expect(session.idleTimeoutSeconds).toBe(1200);
    expect(session.idleWarningSeconds).toBe(90);
  });

  it('uses documented defaults for invalid or missing values', () => {
    process.env.GBAW_SESSION_IDLE_TIMEOUT_SECONDS = 'invalid';
    process.env.GBAW_SESSION_IDLE_WARNING_SECONDS = '0';
    const { req, res } = createMocks({ method: 'GET' });

    handler(req, res);

    const session = JSON.parse(res._getData()).session;
    // 30 minutes idle timeout, 2 minute warning threshold.
    expect(session.idleTimeoutSeconds).toBe(1800);
    expect(session.idleWarningSeconds).toBe(120);
  });

  it('clamps values to the validated minimum and maximum ranges', () => {
    process.env.GBAW_SESSION_IDLE_TIMEOUT_SECONDS = '999999';
    process.env.GBAW_SESSION_IDLE_WARNING_SECONDS = '5';
    const { req, res } = createMocks({ method: 'GET' });

    handler(req, res);

    const session = JSON.parse(res._getData()).session;
    expect(session.idleTimeoutSeconds).toBeLessThanOrEqual(28800);
    expect(session.idleWarningSeconds).toBeGreaterThanOrEqual(30);
  });
});
