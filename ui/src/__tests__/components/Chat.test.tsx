/**
 * Tests for Chat component
 */

import React, { act } from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import { Chat } from '../../components/Chat';

// Helper to setup Portal container for thinking indicator
function setupPortalContainer() {
  const container = document.createElement('div');
  container.className = 'copilotKitMessages';
  document.body.appendChild(container);
  return container;
}

function cleanupPortalContainer(container: HTMLElement) {
  document.body.removeChild(container);
}

// Access the submit counter set by the mock CopilotChat input (see mock below).
function resetSends() {
  const w = window as unknown as { __chatSends?: number };
  w.__chatSends = 0;
}
function getSends(): number {
  return (window as unknown as { __chatSends?: number }).__chatSends ?? 0;
}

// Mock CopilotKit components
jest.mock('@copilotkit/react-core', () => ({
  CopilotKit: ({ children }: { children: React.ReactNode }) => <div data-testid="copilotkit">{children}</div>,
  // Hooks consumed by NewChatButton (#253)
  useCopilotChat: () => ({ reset: jest.fn() }),
  useCopilotContext: () => ({ setThreadId: jest.fn() }),
}));

// Import React at module level for mock
const mockReact = React;

jest.mock('@copilotkit/react-ui', () => ({
  CopilotChat: ({ className, onInProgress, labels, Messages }: {
    className?: string;
    onInProgress?: (inProgress: boolean) => void;
    labels?: { title?: string };
    Messages?: React.ComponentType<{ messages: unknown[]; inProgress: boolean }>;
  }) => {
    const [inProgress, setInProgress] = mockReact.useState(false);

    const handleStart = () => {
      setInProgress(true);
      onInProgress?.(true);
    };

    const handleStop = () => {
      setInProgress(false);
      onInProgress?.(false);
    };

    // A realistic input + submit path: a focusable textarea that submits on a
    // bare Enter (as CopilotKit does) and a send button, so tests exercise the
    // real key/submit surface rather than only markers.
    const bumpSends = () => {
      const w = globalThis as unknown as { __chatSends?: number };
      w.__chatSends = (w.__chatSends ?? 0) + 1;
    };
    const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        bumpSends();
      }
    };
    const onSubmit = (e: React.FormEvent) => {
      e.preventDefault();
      bumpSends();
    };

    return (
      <div data-testid="copilot-chat" className={className}>
        <div data-testid="chat-title">{labels?.title}</div>
        {Messages && <Messages messages={[]} inProgress={inProgress} />}
        <form onSubmit={onSubmit}>
          <textarea data-testid="chat-textarea" placeholder="Ask about game infrastructure..." onKeyDown={onKeyDown} />
          <button data-testid="chat-send" type="submit">Send</button>
        </form>
        <button
          data-testid="trigger-thinking"
          onClick={handleStart}
        >
          Start Thinking
        </button>
        <button
          data-testid="trigger-ready"
          onClick={handleStop}
        >
          Stop Thinking
        </button>
      </div>
    );
  },
}));

describe('Chat', () => {
  it('renders the chat container', () => {
    render(<Chat />);
    expect(screen.getByTestId('copilotkit')).toBeInTheDocument();
    expect(screen.getByTestId('copilot-chat')).toBeInTheDocument();
  });

  it('renders with custom className', () => {
    render(<Chat className="custom-class" />);
    const chat = screen.getByTestId('copilot-chat');
    expect(chat).toHaveClass('ga-chat');
    expect(chat).toHaveClass('custom-class');
  });

  it('displays chat title', () => {
    render(<Chat />);
    expect(screen.getByTestId('chat-title')).toHaveTextContent('🎮 Game Agent');
  });

  it('renders progress bar', () => {
    render(<Chat />);
    const progressBar = document.querySelector('.ga-progress-bar');
    expect(progressBar).toBeInTheDocument();
  });



  it('does not show thinking indicator initially', () => {
    render(<Chat />);
    const thinkingIndicator = document.querySelector('.ga-thinking-indicator');
    expect(thinkingIndicator).not.toBeInTheDocument();
  });

  it('shows thinking indicator when AI is processing', async () => {
    const container = setupPortalContainer();
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    act(() => { startButton.click(); });

    await waitFor(() => {
      const thinkingIndicator = document.querySelector('.ga-thinking-indicator');
      expect(thinkingIndicator).toBeInTheDocument();
    });

    cleanupPortalContainer(container);
  });

  it('shows robot avatar in thinking indicator', async () => {
    const container = setupPortalContainer();
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    act(() => { startButton.click(); });

    await waitFor(() => {
      const avatar = document.querySelector('.ga-thinking-avatar');
      expect(avatar).toHaveTextContent('🤖');
    });

    cleanupPortalContainer(container);
  });

  it('shows "Analyzing your request..." text when thinking', async () => {
    const container = setupPortalContainer();
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    act(() => { startButton.click(); });

    await waitFor(() => {
      const text = document.querySelector('.ga-thinking-text');
      expect(text).toHaveTextContent('Analyzing your request...');
    });

    cleanupPortalContainer(container);
  });

  it('shows three animated dots when thinking', async () => {
    const container = setupPortalContainer();
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    act(() => { startButton.click(); });

    await waitFor(() => {
      const dots = document.querySelectorAll('.ga-thinking-dot');
      expect(dots).toHaveLength(3);
    });

    cleanupPortalContainer(container);
  });



  it('adds "active" class to progress bar when processing', async () => {
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    act(() => { startButton.click(); });

    await waitFor(() => {
      const progressBar = document.querySelector('.ga-progress-bar');
      expect(progressBar).toHaveClass('active');
    });
  });

  it('hides thinking indicator when AI finishes', async () => {
    const container = setupPortalContainer();
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    const stopButton = screen.getByTestId('trigger-ready');

    act(() => { startButton.click(); });

    await waitFor(() => {
      expect(document.querySelector('.ga-thinking-indicator')).toBeInTheDocument();
    });

    act(() => { stopButton.click(); });

    await waitFor(() => {
      expect(document.querySelector('.ga-thinking-indicator')).not.toBeInTheDocument();
    });

    cleanupPortalContainer(container);
  });



  it('removes "active" class from progress bar when finished', async () => {
    render(<Chat />);

    const startButton = screen.getByTestId('trigger-thinking');
    const stopButton = screen.getByTestId('trigger-ready');

    act(() => { startButton.click(); });
    await waitFor(() => {
      expect(document.querySelector('.ga-progress-bar')).toHaveClass('active');
    });

    act(() => { stopButton.click(); });
    await waitFor(() => {
      expect(document.querySelector('.ga-progress-bar')).not.toHaveClass('active');
    });
  });

  it('does not block interaction when enabled (default)', () => {
    render(<Chat />);
    expect(document.querySelector('.ga-chat-disabled-overlay')).not.toBeInTheDocument();
    const wrapper = document.querySelector('.ga-chat-wrapper');
    expect(wrapper).not.toHaveAttribute('aria-disabled', 'true');
    // The input is usable when enabled.
    const textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(false);
  });

  it('functionally disables the textarea and blocks Enter submission once logout begins', () => {
    resetSends();
    render(<Chat disabled />);

    // Semantics preserved: overlay + aria-disabled wrapper.
    expect(document.querySelector('.ga-chat-disabled-overlay')).toBeInTheDocument();
    expect(document.querySelector('.ga-chat-wrapper')).toHaveAttribute('aria-disabled', 'true');

    const textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    // The ACTUAL input is disabled, not merely visually covered.
    expect(textarea.disabled).toBe(true);
    expect(textarea).toHaveAttribute('aria-disabled', 'true');

    // Even if focus is forced onto the input, a bare Enter must not submit.
    textarea.focus();
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(getSends()).toBe(0);
  });

  it('blocks a programmatic submit/click while disabled', () => {
    resetSends();
    render(<Chat disabled />);
    const send = screen.getByTestId('chat-send');
    fireEvent.click(send);
    fireEvent.submit(send.closest('form') as HTMLFormElement);
    expect(getSends()).toBe(0);
  });

  it('preserves transcript accessibility while disabled (transcript stays in the tree)', () => {
    render(<Chat disabled />);
    // The overlay is aria-hidden so it does not swallow the transcript for AT,
    // and the chat container itself is still present/readable.
    const overlay = document.querySelector('.ga-chat-disabled-overlay');
    expect(overlay).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByTestId('copilot-chat')).toBeInTheDocument();
    expect(screen.getByTestId('chat-title')).toHaveTextContent('🎮 Game Agent');
  });

  it('re-enables the input when logout state clears (guard is reversible)', () => {
    resetSends();
    const { rerender } = render(<Chat disabled />);
    let textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(true);

    rerender(<Chat disabled={false} />);
    textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(false);
    // Submission works again once re-enabled.
    fireEvent.submit(textarea.closest('form') as HTMLFormElement);
    expect(getSends()).toBe(1);
  });

});
