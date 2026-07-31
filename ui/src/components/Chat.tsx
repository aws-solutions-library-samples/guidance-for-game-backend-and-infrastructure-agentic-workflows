/**
 * Chat Component - Enhanced with thinking indicators
 * Following CopilotKit best practices from their examples
 */

"use client";

import React, { useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import { NewChatButton } from "./NewChatButton";

// Progress messages for rotating indicator
const PROGRESS_MESSAGES = [
  "Analyzing your request...",
  "Consulting AWS services...",
  "Processing infrastructure data...",
  "Gathering insights...",
  "Generating response...",
  "Almost there...",
  "Still processing, thanks for your patience..."
];

/**
 * Main chat component with proper SSR handling and enhanced UX
 */
interface ChatProps {
  className?: string;
  onThinkingChange?: (isThinking: boolean) => void;
}

export function Chat({ className, onThinkingChange }: ChatProps) {
  const [isThinking, setIsThinking] = useState(false);
  const [messagesContainer, setMessagesContainer] = useState<Element | null>(null);
  const [messageIndex, setMessageIndex] = useState(0);
  const progressMessage = PROGRESS_MESSAGES[Math.min(messageIndex, PROGRESS_MESSAGES.length - 1)];

  // Wrap CopilotChat's progress callback so the rotation resets at the start of
  // each thinking session. Resetting here (an event handler) instead of inside
  // an effect avoids a synchronous setState-in-effect cascade.
  const handleInProgress = (thinking: boolean) => {
    if (thinking) {
      setMessageIndex(0);
    }
    setIsThinking(thinking);
  };

  // Rotate progress messages while thinking. setMessageIndex runs only inside the
  // interval callback (async), never synchronously in the effect body.
  useEffect(() => {
    if (!isThinking) {
      return;
    }

    const interval = setInterval(() => {
      setMessageIndex((index) => {
        const next = index + 1;
        if (next >= PROGRESS_MESSAGES.length) {
          // Stop at last message - don't loop back
          clearInterval(interval);
          return index;
        }
        return next;
      });
    }, 3000); // Rotate every 3 seconds

    return () => clearInterval(interval);
  }, [isThinking]);

  // Notify parent when thinking state changes
  useEffect(() => {
    if (onThinkingChange) {
      onThinkingChange(isThinking);
    }
  }, [isThinking, onThinkingChange]);

  // Find the messages container for Portal. CopilotChat renders it on mount, so
  // poll until present and stop once found; setMessagesContainer runs inside the
  // interval callback rather than synchronously in the effect body.
  useEffect(() => {
    const interval = setInterval(() => {
      const container = document.querySelector('.copilotKitMessages');
      if (container) {
        setMessagesContainer(container);
        clearInterval(interval);
      }
    }, 50);

    return () => clearInterval(interval);
  }, []);

  return (
    <>
      {/* Progress bar - React managed */}
      <div className={`ga-progress-bar ${isThinking ? 'active' : ''}`} />

      <CopilotKit
        runtimeUrl="/api/copilot/chat"
        showDevConsole={false}
      >
        <div className="ga-chat-wrapper">
          {/* Must live inside the CopilotKit provider: it consumes the chat
              context to reset messages AND rotate the threadId (#253). */}
          <NewChatButton />
          <CopilotChat
            className={`ga-chat ${className || ''}`}
            onInProgress={handleInProgress}
            labels={{
            title: "🎮 Game Agent",
            initial: `**Welcome to Game Agent!** 🎮

Your AI assistant for game server management, with **persistent memory** across sessions. I can help with **GameLift** fleets, **EKS/Kubernetes** operations, **cost optimization**, health monitoring, and security.

Try asking:
- List my GameLift fleets
- List my EKS clusters
- How much am I spending on GameLift?

**What would you like to explore first?**`,
            placeholder: "Ask about game infrastructure...",
          }}
          instructions={`You are Game Agent, an expert AI assistant for game server management and AWS infrastructure.

You specialize in:
- AWS GameLift fleet management and optimization
- EKS/Kubernetes cluster operations and troubleshooting
- Cost analysis and optimization strategies
- Infrastructure health monitoring and alerting
- Security best practices and compliance

You have access to conversation history and can reference previous messages. Always provide:
- Specific, actionable recommendations
- Real data when available (fleet IDs, cluster names, cost figures)
- Professional analysis with clear next steps
- Context-aware responses that build on previous discussions

Be concise, informative, and maintain conversation context throughout our discussion.`}
          makeSystemMessage={(instructions) =>
            instructions + "\n\nIMPORTANT: You have access to full conversation history. Reference previous messages and maintain context across the entire conversation."
          }
        />

        {/* Thinking indicator - rendered via Portal into messages container */}
        {isThinking && messagesContainer && createPortal(
          <div className="ga-thinking-overlay">
            <div className="ga-thinking-indicator">
              <div className="ga-thinking-bubble">
                <div className="ga-thinking-avatar">🤖</div>
                <div className="ga-thinking-dots">
                  <div className="ga-thinking-dot"></div>
                  <div className="ga-thinking-dot"></div>
                  <div className="ga-thinking-dot"></div>
                </div>
                <div className="ga-thinking-text">{progressMessage}</div>
              </div>
            </div>
          </div>,
          messagesContainer
        )}
        </div>
      </CopilotKit>
    </>
  );
}
