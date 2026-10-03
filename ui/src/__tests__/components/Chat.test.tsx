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
    // Retained React text state, exactly like the installed CopilotKit Input:
    // the send button stays enabled while there is non-empty text even after the
    // DOM textarea is disabled by the guard.
    const [text, setText] = mockReact.useState('has text');

    const handleStart = () => {
      setInProgress(true);
      onInProgress?.(true);
    };

    const handleStop = () => {
      setInProgress(false);
      onInProgress?.(false);
    };

    // Faithfully model the installed @copilotkit/react-ui 1.10.6 sender: a PLAIN
    // button with onClick={send} (NOT a form submit), and a textarea whose bare
    // Enter calls send() directly from onKeyDown. See
    // node_modules/@copilotkit/react-ui/src/components/chat/Input.tsx.
    const bumpSends = () => {
      const w = globalThis as unknown as { __chatSends?: number };
      w.__chatSends = (w.__chatSends ?? 0) + 1;
    };
    const send = () => {
      if (inProgress) return;
      bumpSends();
    };
    const canSend = text.trim().length > 0 && !inProgress;
    const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (canSend) send();
      }
    };

    return (
      <div data-testid="copilot-chat" className={className}>
        <div data-testid="chat-title">{labels?.title}</div>
        {Messages && <Messages messages={[]} inProgress={inProgress} />}
        <div className="copilotKitInput">
          <textarea
            data-testid="chat-textarea"
            placeholder="Ask about game infrastructure..."
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="copilotKitInputControls">
            <button
              data-testid="chat-send"
              className="copilotKitInputControlButton"
              disabled={!canSend}
              onClick={send}
            >
              Send
            </button>
          </div>
        </div>
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
    // The input and send button are usable when enabled.
    const textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(false);
    const send = screen.getByTestId('chat-send') as HTMLButtonElement;
    expect(send.disabled).toBe(false);
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

    // Even if focus is forced onto the input, a bare Enter must not send
    // (CopilotKit's onKeyDown calls send() directly — the capture guard wins).
    textarea.focus();
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(getSends()).toBe(0);
  });

  it('disables and blocks the REAL CopilotKit send button (plain onClick) while disabled', () => {
    resetSends();
    render(<Chat disabled />);
    const send = screen.getByTestId('chat-send') as HTMLButtonElement;

    // The installed sender is a plain button with onClick={send} and retained
    // text state; it must be functionally disabled, not just covered.
    expect(send.disabled).toBe(true);
    expect(send).toHaveAttribute('aria-disabled', 'true');

    // A programmatic button.click() must not send (capture-phase click guard +
    // disabled button).
    send.click();
    expect(getSends()).toBe(0);

    // A synthesized click event likewise cannot reach the plain onClick sender.
    fireEvent.click(send);
    expect(getSends()).toBe(0);

    // Keyboard activation on the focused send button (Space/Enter) is blocked.
    send.focus();
    fireEvent.keyDown(send, { key: ' ' });
    fireEvent.keyDown(send, { key: 'Enter' });
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

  it('re-enables the input and send button when logout state clears (guard is reversible)', () => {
    resetSends();
    const { rerender } = render(<Chat disabled />);
    let textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    let send = screen.getByTestId('chat-send') as HTMLButtonElement;
    expect(textarea.disabled).toBe(true);
    expect(send.disabled).toBe(true);

    rerender(<Chat disabled={false} />);
    textarea = screen.getByTestId('chat-textarea') as HTMLTextAreaElement;
    send = screen.getByTestId('chat-send') as HTMLButtonElement;
    expect(textarea.disabled).toBe(false);
    expect(send.disabled).toBe(false);
    // Sending works again once re-enabled: a plain button click sends.
    send.click();
    expect(getSends()).toBe(1);
  });

});
