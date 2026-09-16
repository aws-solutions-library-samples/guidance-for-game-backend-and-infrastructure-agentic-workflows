import {
  refreshSessionOnce,
  resetRefreshCoordinatorForTests,
  subscribeToSessionExpiration,
} from '@/utils/sessionRefresh';

class MockBroadcastChannel {
  static instances: MockBroadcastChannel[] = [];
  onmessage: ((event: MessageEvent<{ type?: string }>) => void) | null = null;
  postMessage = jest.fn();
  close = jest.fn();

  constructor(public name: string) {
    MockBroadcastChannel.instances.push(this);
  }
}

describe('session refresh coordinator', () => {
  beforeEach(() => {
    resetRefreshCoordinatorForTests();
    window.localStorage.clear();
    MockBroadcastChannel.instances = [];
    Object.defineProperty(global, 'BroadcastChannel', {
      configurable: true,
      writable: true,
      value: MockBroadcastChannel,
    });
    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: undefined,
    });
  });

  it('deduplicates concurrent refresh attempts in one tab', async () => {
    let release!: () => void;
    const response = new Promise<Response>((resolve) => {
      release = () => resolve({ ok: true, status: 200 } as Response);
    });
    const fetchMock = jest.fn(() => response) as unknown as typeof window.fetch;

    const first = refreshSessionOnce(fetchMock);
    const second = refreshSessionOnce(fetchMock);
    release();

    await expect(Promise.all([first, second])).resolves.toEqual([true, true]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/refresh',
      expect.objectContaining({ method: 'POST', signal: expect.anything() }),
    );
  });

  it('fails closed when refresh is rejected', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: false, status: 401 });
    await expect(refreshSessionOnce(fetchMock)).resolves.toBe(false);
    const publisher = MockBroadcastChannel.instances.find((channel) =>
      channel.postMessage.mock.calls.some(([message]) => message.type === 'expired'));
    expect(publisher).toBeDefined();
  });

  it('reuses a refresh completed by another tab during the cooldown', async () => {
    window.localStorage.setItem('game-agent-last-session-refresh', String(Date.now()));
    const fetchMock = jest.fn();
    await expect(refreshSessionOnce(fetchMock)).resolves.toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('notifies subscribers when another tab reports expiration', () => {
    const onExpired = jest.fn();
    const unsubscribe = subscribeToSessionExpiration(onExpired);
    const subscriber = MockBroadcastChannel.instances[0];

    subscriber.onmessage?.({ data: { type: 'expired' } } as MessageEvent<{ type?: string }>);

    expect(onExpired).toHaveBeenCalledTimes(1);
    unsubscribe();
    expect(subscriber.close).toHaveBeenCalled();
  });
});
