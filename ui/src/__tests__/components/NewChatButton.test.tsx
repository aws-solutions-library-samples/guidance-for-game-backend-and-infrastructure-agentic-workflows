/**
 * Tests for NewChatButton (issue #253).
 *
 * The critical contract: a "New chat" must clear BOTH the client-side messages
 * AND rotate the threadId. Conversation memory lives server-side in AgentCore,
 * keyed by threadId (→ runtimeSessionId). Calling reset() alone clears the
 * visible messages but keeps the same threadId, so the server keeps injecting
 * the old conversation — a vacuous "new chat" that silently fails its purpose.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import { NewChatButton } from '../../components/NewChatButton';

const mockReset = jest.fn();
const mockSetThreadId = jest.fn();

jest.mock('@copilotkit/react-core', () => ({
  useCopilotChat: () => ({ reset: mockReset }),
  useCopilotContext: () => ({ setThreadId: mockSetThreadId }),
}));

describe('NewChatButton (#253)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders an accessible button', () => {
    render(<NewChatButton />);
    expect(screen.getByRole('button', { name: /new chat/i })).toBeInTheDocument();
  });

  it('clears the client messages when clicked', async () => {
    render(<NewChatButton />);
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }));
    expect(mockReset).toHaveBeenCalledTimes(1);
  });

  it('rotates the threadId so the server-side AgentCore session is fresh', async () => {
    render(<NewChatButton />);
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }));

    // Without this, reset() alone leaves the same runtimeSessionId and the old
    // conversation keeps being replayed — the whole point of the button fails.
    expect(mockSetThreadId).toHaveBeenCalledTimes(1);
    const newThreadId = mockSetThreadId.mock.calls[0][0];
    expect(typeof newThreadId).toBe('string');
    expect(newThreadId.length).toBeGreaterThan(0);
  });

  it('generates a distinct threadId on each click', async () => {
    render(<NewChatButton />);
    const button = screen.getByRole('button', { name: /new chat/i });
    await userEvent.click(button);
    await userEvent.click(button);

    expect(mockSetThreadId).toHaveBeenCalledTimes(2);
    const first = mockSetThreadId.mock.calls[0][0];
    const second = mockSetThreadId.mock.calls[1][0];
    expect(first).not.toBe(second);
  });
});
