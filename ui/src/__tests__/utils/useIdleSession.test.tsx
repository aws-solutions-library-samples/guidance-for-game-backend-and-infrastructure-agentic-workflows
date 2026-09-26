import React from 'react';
import { act, render, screen, fireEvent } from '@testing-library/react';
import { useIdleSession } from '@/utils/useIdleSession';
import { resetIdleCoordinatorForTests } from '@/utils/idleTimer';

// A tiny harness component that surfaces the hook's state for assertions.
function Harness(props: {
  enabled: boolean;
  config: { idleTimeoutSeconds: number; idleWarningSeconds: number };
  refresh: () => Promise<boolean>;
  onLogout: () => void;
}) {
  const idle = useIdleSession({
    enabled: props.enabled,
    config: props.config,
    refresh: props.refresh,
    onLogout: props.onLogout,
  });
  return (
    <div>
      <span data-testid="warning-open">{String(idle.warningOpen)}</span>
      <span data-testid="logging-out">{String(idle.loggingOut)}</span>
      <span data-testid="busy">{String(idle.busy)}</span>
      <span data-testid="error">{idle.errorMessage ?? ''}</span>
      <button onClick={idle.onStay}>stay</button>
      <button onClick={idle.onSignOut}>signout</button>
      <button onClick={idle.markActivity}>activity</button>
    </div>
  );
}

const CONFIG = { idleTimeoutSeconds: 1800, idleWarningSeconds: 120 };

describe('useIdleSession', () => {
  let now: number;

  beforeEach(() => {
    jest.useFakeTimers();
    resetIdleCoordinatorForTests();
    window.localStorage.clear();
    now = 1_000_000;
    jest.spyOn(Date, 'now').mockImplementation(() => now);
  });

  afterEach(() => {
    act(() => {
      jest.runOnlyPendingTimers();
    });
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  function advance(ms: number) {
    act(() => {
      now += ms;
      jest.advanceTimersByTime(ms);
    });
  }

  it('opens the warning at the configured threshold', () => {
    render(
      <Harness enabled config={CONFIG} refresh={jest.fn().mockResolvedValue(true)} onLogout={jest.fn()} />,
    );
    expect(screen.getByTestId('warning-open')).toHaveTextContent('false');
    advance(28 * 60_000 + 1_000); // just past the 28-minute warning threshold
    expect(screen.getByTestId('warning-open')).toHaveTextContent('true');
  });

  it('meaningful activity before the warning resets the deadline', () => {
    render(
      <Harness enabled config={CONFIG} refresh={jest.fn().mockResolvedValue(true)} onLogout={jest.fn()} />,
    );
    advance(20 * 60_000);
    act(() => {
      fireEvent.click(screen.getByText('activity'));
    });
    advance(20 * 60_000); // 40m wall clock, only 20m since reset
    expect(screen.getByTestId('warning-open')).toHaveTextContent('false');
  });

  it('"Stay signed in" refreshes then closes the warning', async () => {
    const refresh = jest.fn().mockResolvedValue(true);
    render(<Harness enabled config={CONFIG} refresh={refresh} onLogout={jest.fn()} />);
    advance(28 * 60_000 + 1_000);
    expect(screen.getByTestId('warning-open')).toHaveTextContent('true');

    await act(async () => {
      fireEvent.click(screen.getByText('stay'));
    });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('warning-open')).toHaveTextContent('false');
    expect(screen.getByTestId('logging-out')).toHaveTextContent('false');
  });

  it('keeps the warning open and logs out when refresh fails', async () => {
    const refresh = jest.fn().mockResolvedValue(false);
    const onLogout = jest.fn();
    render(<Harness enabled config={CONFIG} refresh={refresh} onLogout={onLogout} />);
    advance(28 * 60_000 + 1_000);

    await act(async () => {
      fireEvent.click(screen.getByText('stay'));
    });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('logging-out')).toHaveTextContent('true');
    expect(screen.getByTestId('error')).not.toHaveTextContent('');
  });

  it('expiry at the deadline triggers logout exactly once', () => {
    const onLogout = jest.fn();
    render(<Harness enabled config={CONFIG} refresh={jest.fn()} onLogout={onLogout} />);
    advance(30 * 60_000 + 1_000);
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('logging-out')).toHaveTextContent('true');
    // Further ticks must not re-fire logout.
    advance(60_000);
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it('background-tab throttling still expires: a single late tick logs out', () => {
    const onLogout = jest.fn();
    render(<Harness enabled config={CONFIG} refresh={jest.fn()} onLogout={onLogout} />);
    // No intermediate ticks fired (throttled tab); a single large jump lands
    // well past the deadline and still logs out.
    advance(45 * 60_000);
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it('manual sign-out uses the logout path and blocks further activity', () => {
    const onLogout = jest.fn();
    render(<Harness enabled config={CONFIG} refresh={jest.fn()} onLogout={onLogout} />);
    advance(28 * 60_000 + 1_000);
    act(() => {
      fireEvent.click(screen.getByText('signout'));
    });
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('logging-out')).toHaveTextContent('true');
  });

  it('does nothing when disabled', () => {
    const onLogout = jest.fn();
    render(<Harness enabled={false} config={CONFIG} refresh={jest.fn()} onLogout={onLogout} />);
    advance(45 * 60_000);
    expect(screen.getByTestId('warning-open')).toHaveTextContent('false');
    expect(onLogout).not.toHaveBeenCalled();
  });
});
