/**
 * Chat Component - Enhanced with thinking indicators
 * Following CopilotKit best practices from their examples
 */

"use client";

import React, { useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";

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
  const [progressMessage, setProgressMessage] = useState(PROGRESS_MESSAGES[0]);

  // Rotate progress messages while thinking
  useEffect(() => {
    if (!isThinking) {
      setProgressMessage(PROGRESS_MESSAGES[0]); // Reset to first message
      return;
    }

    let messageIndex = 0;
    const interval = setInterval(() => {
      messageIndex++;
      if (messageIndex < PROGRESS_MESSAGES.length) {
        setProgressMessage(PROGRESS_MESSAGES[messageIndex]);
      } else {
        // Stop at last message - don't loop back
        clearInterval(interval);
      }
    }, 3000); // Rotate every 3 seconds

    return () => clearInterval(interval);
  }, [isThinking]);

  // Notify parent when thinking state changes
  useEffect(() => {
    if (onThinkingChange) {
      onThinkingChange(isThinking);
    }
  }, [isThinking, onThinkingChange]);

  // Find the messages container for Portal (only once)
  useEffect(() => {
    const container = document.querySelector('.copilotKitMessages');
    setMessagesContainer(container);
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
          <CopilotChat
            className={`ga-chat ${className || ''}`}
            onInProgress={setIsThinking}
            labels={{
            title: "🎮 Game Agent",
            initial: `**Welcome to Game Agent!** 🎮

I'm your AI-powered game server management assistant with **persistent memory**.

**🧠 Memory Features:**
- I remember our conversations across sessions
- I learn your infrastructure preferences
- I build context about your AWS environment
- I provide personalized recommendations

**I can help you with:**
- **GameLift Fleet Management** - Monitor, scale, and optimize your game servers
- **EKS/Kubernetes Operations** - Manage clusters, pods, and deployments
- **Cost Analysis & Optimization** - Track spending and find savings opportunities
- **Health Monitoring** - Real-time system status and performance metrics
- **Security & Compliance** - Best practices and vulnerability assessments

I maintain context across all our interactions, so feel free to reference previous conversations!

For instance, you can ask me:
- What can you help me with?
- List my GameLift fleets
- List my EKS clusters
- How much am I spending on GameLift?

**What would you like to explore first?**`,
            placeholder: "Ask about your game servers, costs, or infrastructure...",
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
