/**
 * Tests for Chat component
 */

import React, { act } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
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

// Mock CopilotKit components
jest.mock('@copilotkit/react-core', () => ({
  CopilotKit: ({ children }: { children: React.ReactNode }) => <div data-testid="copilotkit">{children}</div>,
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

    return (
      <div data-testid="copilot-chat" className={className}>
        <div data-testid="chat-title">{labels?.title}</div>
        {Messages && <Messages messages={[]} inProgress={inProgress} />}
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

});
