/**
 * NewChatButton - clears the conversation and starts a fresh session (#253).
 *
 * Must be rendered inside the <CopilotKit> provider.
 *
 * Two calls are required, not one:
 * - reset() clears the client-side messages and run state.
 * - setThreadId(<new uuid>) rotates the CopilotKit threadId, which the chat
 *   proxy maps to the AgentCore runtimeSessionId. Without this, the server-side
 *   session keeps replaying the old conversation into every new turn — the chat
 *   LOOKS empty but the context was never cleared.
 *
 * Long-term user memory (keyed by runtimeUserId) is intentionally untouched:
 * "new chat" clears the conversation, not what the agent knows about the user.
 */

"use client";

import React from "react";
import { useCopilotChat, useCopilotContext } from "@copilotkit/react-core";

// Platform crypto.randomUUID (all modern browsers / Node 19+), with a Math.random
// fallback for older jsdom. Deliberately NOT @copilotkit/shared's randomUUID —
// importing that module pulls in its Segment telemetry client.
function newThreadId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `thread-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function NewChatButton() {
  const { reset } = useCopilotChat();
  const { setThreadId } = useCopilotContext();

  const handleNewChat = () => {
    reset();
    setThreadId(newThreadId());

    // CopilotKit keeps the message scroller mounted when reset() replaces the
    // conversation. Clear its old offset so the welcome message is not clipped.
    const messages = document.querySelector<HTMLElement>(".copilotKitMessages");
    if (messages) {
      messages.scrollTop = 0;
    }
  };

  return (
    <button
      type="button"
      className="ga-new-chat-button"
      onClick={handleNewChat}
      aria-label="New chat"
      title="Start a new chat (clears the conversation)"
    >
      <span aria-hidden="true">＋</span> New chat
    </button>
  );
}
