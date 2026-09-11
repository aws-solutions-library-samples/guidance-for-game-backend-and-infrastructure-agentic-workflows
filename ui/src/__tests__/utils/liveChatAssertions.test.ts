import { assertHealthySpecialistReply } from '../../../tests/helpers/reply-contract';

describe('authenticated live-chat response contract', () => {
  it('accepts an on-topic successful reply', () => {
    expect(() => assertHealthySpecialistReply('Fleet fleet-1 is ACTIVE.', ['fleet'], 'GameLift')).not.toThrow();
  });

  it.each([
    "I'm sorry, but your request could not be processed due to an identity verification issue.",
    'Internal server error',
    'Unable to reach the MCP server',
    'Something went wrong and I could not complete that request.',
    'Access denied while retrieving cost data.',
    'The request timed out while listing clusters.',
    'The service is temporarily unavailable because it was throttled.',
    'I could not list your GameLift fleets.',
  ])('rejects visible runtime failures: %s', (reply) => {
    expect(() => assertHealthySpecialistReply(reply, ['fleet'], 'GameLift')).toThrow(/GameLift/);
  });

  it.each([
    'I can explain EKS clusters, but no live data was checked.',
    "I don't have access to your EKS clusters.",
    'I do not have access to your EKS clusters.',
    'I was unable to retrieve your EKS clusters.',
    'I did not retrieve live EKS cluster data.',
    'I was not able to inspect your EKS clusters.',
    'I couldn\u2019t inspect your EKS clusters.',
  ])('rejects an on-topic response that does not prove live data access: %s', (reply) => {
    expect(() => assertHealthySpecialistReply(reply, ['cluster'], 'EKS')).toThrow(/EKS/);
  });

  it('rejects a successful-looking but off-topic reply', () => {
    expect(() => assertHealthySpecialistReply('Your request completed successfully.', ['cluster'], 'EKS')).toThrow(
      /off-topic/,
    );
  });
});
