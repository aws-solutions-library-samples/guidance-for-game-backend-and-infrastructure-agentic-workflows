import type { NextApiRequest, NextApiResponse } from 'next';
import { logInfo, logError, logDebug, redact } from '@/utils/logger';
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from '@aws-sdk/client-bedrock-agentcore';
import { STSClient, GetCallerIdentityCommand } from '@aws-sdk/client-sts';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { parse } from 'cookie';

// JWT verifiers for production (lazy-initialized). Separate verifiers per token
// use: aws-jwt-verify enforces the token_use claim, so the access token must be
// verified with an 'access' verifier and the id token with an 'id' verifier.
let accessVerifier: ReturnType<typeof CognitoJwtVerifier.create> | null = null;
let idVerifier: ReturnType<typeof CognitoJwtVerifier.create> | null = null;

function getAccessVerifier() {
  if (!accessVerifier) {
    accessVerifier = CognitoJwtVerifier.create({
      userPoolId: process.env.COGNITO_USER_POOL_ID!,
      tokenUse: 'access',
      clientId: process.env.COGNITO_CLIENT_ID!,
    });
  }
  return accessVerifier;
}

function getIdVerifier() {
  if (!idVerifier) {
    idVerifier = CognitoJwtVerifier.create({
      userPoolId: process.env.COGNITO_USER_POOL_ID!,
      tokenUse: 'id',
      clientId: process.env.COGNITO_CLIENT_ID!,
    });
  }
  return idVerifier;
}

/**
 * Verify the access-token cookie and return its decoded payload (or null).
 *
 * In non-production (local dev) auth is skipped — callers treat a null payload
 * in dev as "authenticated, no verified identity". The returned `sub` is used to
 * bind the id token to this access token so a valid access token cannot be paired
 * with a forged id token.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function verifyAccessToken(req: NextApiRequest): Promise<{ ok: boolean; payload: any }> {
  // Skip JWT validation in development
  if (process.env.NODE_ENV !== 'production') {
    return { ok: true, payload: null };
  }

  const cookies = parse(req.headers.cookie || '');
  const token = cookies.cognito_access_token;
  if (!token) {
    return { ok: false, payload: null };
  }

  try {
    const payload = await getAccessVerifier().verify(token);
    return { ok: true, payload };
  } catch (error) {
    logError('Access token validation failed:', error instanceof Error ? error : new Error(String(error)));
    return { ok: false, payload: null };
  }
}

// Cache account ID to avoid repeated STS calls
let cachedAccountId: string = '';
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let cachedUserIdentity: any = null;

async function getAccountId(): Promise<string> {
  if (cachedAccountId) {
    return cachedAccountId;
  }

  const sts = new STSClient({ region: process.env.AWS_REGION || 'us-west-2' });
  const identity = await sts.send(new GetCallerIdentityCommand({}));
  cachedAccountId = identity.Account!;
  return cachedAccountId;
}

// Enhanced user identity extraction with AWS principal
async function getUserIdentity() {

  // Development: Use AWS principal information (cached)
  if (!process.env.AGENTCORE_RUNTIME_ID) {
    if (cachedUserIdentity) {
      return cachedUserIdentity;
    }

    try {
      const sts = new STSClient({ region: process.env.AWS_REGION || 'us-west-2' });
      const identity = await sts.send(new GetCallerIdentityCommand({}));

      // Extract username from ARN
      const arn = identity.Arn || '';
      const accountId = identity.Account || '';

      // Parse different ARN formats to extract username
      let username = 'dev-user';
      let displayName = 'Developer';

      if (arn.includes(':user/')) {
        // IAM User: arn:aws:iam::123456789012:user/john.doe
        username = arn.split(':user/')[1] || 'dev-user';
        displayName = username.replace(/[._-]/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
      } else if (arn.includes(':assumed-role/')) {
        // Assumed Role: arn:aws:sts::123456789012:assumed-role/MyRole/john.doe
        const parts = arn.split('/');
        if (parts.length >= 3) {
          username = parts[parts.length - 1]; // Last part is usually the session name/username
          displayName = username.replace(/[._-]/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
        }
      } else if (arn.includes(':root')) {
        // Root user: arn:aws:iam::123456789012:root
        username = 'root';
        displayName = 'Root User';
      }

      // Create a stable user ID combining account and username
      const userId = `aws-${accountId}-${username}`;

      cachedUserIdentity = {
        userId: userId,                    // Persistent: aws-123456789012-john.doe
        email: `${username}@${accountId}.aws.local`,
        username: username,               // john.doe
        displayName: displayName,         // John Doe
        accountId: accountId,
        arn: arn,
        authType: 'aws-credentials'
      };

      return cachedUserIdentity;
    } catch (error) {
      logError('Failed to get AWS principal information', error instanceof Error ? error : new Error(String(error)));
      // Fallback for dev
      return {
        userId: 'dev-anonymous',
        email: 'dev@localhost',
        username: 'dev-user',
        displayName: 'Developer',
        authType: 'aws-credentials'
      };
    }
  }

  // Production: Return null, will be set from Cognito JWT in main handler
  return null;
}

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  const requestId = Math.random().toString(36).substring(2, 15);
  const sanitizedMethod = req.method?.replace(/[\r\n]/g, '') || 'UNKNOWN';

  try {
    logInfo(`[${requestId}] ========================================`);
    logInfo(`[${requestId}] 🚀 COPILOT PROXY - NEW REQUEST`);
    logInfo(`[${requestId}] ========================================`);
    logInfo(`[${requestId}] Received ${sanitizedMethod} request to /api/copilot/chat`);

    // Validate JWT in production
    const { ok: isAuthenticated, payload: accessPayload } = await verifyAccessToken(req);
    if (!isAuthenticated) {
      logError(`[${requestId}] ❌ Unauthorized: Invalid or missing JWT token`);
      return res.status(401).json({ error: 'Unauthorized', requestId });
    }

    // Extract user context from the ID token. The ID token is CRYPTOGRAPHICALLY
    // VERIFIED (signature + token_use + expiry) — not raw-decoded — because its
    // claims (cognito:groups, sub) drive the approval gate and the isAdmin flag.
    // It is also bound to the verified access token (matching sub) so a valid
    // access token can't be paired with a forged/other id token. (#117)
    const cookies = parse(req.headers.cookie || '');
    const idToken = cookies.cognito_id_token;
    let userContext = {};
    // Verified ID-token claims, reused later for the memory-identity block so the
    // token is verified exactly once (no second raw decode).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let verifiedIdClaims: any = null;

    if (idToken) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let decoded: any;
      try {
        decoded = await getIdVerifier().verify(idToken);
        verifiedIdClaims = decoded;
      } catch (error) {
        logError(`[${requestId}] ❌ ID token verification failed`, error instanceof Error ? error : new Error(String(error)));
        return res.status(401).json({ error: 'Invalid token', requestId });
      }

      // Bind id token to the verified access token (production only — in dev the
      // access token is not verified, so there is no sub to bind against).
      if (accessPayload?.sub && decoded.sub !== accessPayload.sub) {
        logError(`[${requestId}] ❌ Token binding mismatch (id sub != access sub)`);
        return res.status(401).json({ error: 'Token mismatch', requestId });
      }

      const groups = (decoded['cognito:groups'] as string[]) || [];

      // Check if user is confirmed and approved
      if (!groups.includes('admin') && !groups.includes('users')) {
        logError(`[${requestId}] ❌ User not approved`);
        return res.status(403).json({
          error: 'Account pending approval',
          message: 'Your account is awaiting admin approval. Please contact an administrator.',
          requestId
        });
      }

      userContext = {
        userId: decoded.sub,
        email: decoded.email,
        isAdmin: groups.includes('admin'),
        groups: groups
      };

      logInfo(`[${requestId}] 👤 User: ${redact(decoded.email as string)}, Admin: ${groups.includes('admin')}`);
    }

    // Handle different HTTP methods appropriately
    if (req.method === 'GET' || req.method === 'HEAD') {
      logInfo(`[${requestId}] 🔍 ${sanitizedMethod} request - testing backend connectivity`);

      try {
        // Test backend connectivity using AgentCore Runtime endpoint
        const backendUrl = process.env.BACKEND_URL || 'http://localhost:8080';

        const headers: Record<string, string> = {
          'X-Request-ID': requestId,
        };

        // Add JWT authentication for production
        if (!!process.env.AGENTCORE_RUNTIME_ID) {
          headers['Authorization'] = 'Bearer mock-jwt-token-for-testing';
        }

        const response = await fetch(`${backendUrl}/ping`, {
          method: 'GET',
          headers,
        });

        logInfo(`[${requestId}] ✅ Backend connectivity test: ${response.status}`);
        return res.status(response.status).json({ status: 'connected', requestId });
      } catch (error) {
        logError(`[${requestId}] ❌ Backend connectivity test failed:`, error instanceof Error ? error : new Error(String(error)));
        return res.status(503).json({
          status: 'disconnected',
          error: 'Backend unavailable',
          requestId
        });
      }
    }

    // Handle POST requests (actual chat requests)
    if (req.method !== 'POST') {
      logError(`[${requestId}] ❌ Unsupported method: ${sanitizedMethod}`);
      return res.status(405).json({ error: 'Method not allowed', requestId });
    }
    logInfo(`[${requestId}] 🔄 Processing chat request`);
    logInfo(`[${requestId}] 📦 Full request body keys: ${Object.keys(req.body).join(', ')}`);

    // CopilotKit sends GraphQL-style requests
    const operation = req.body.operationName;
    const variables = req.body.variables || {};
    const data = variables.data || {};

    logInfo(`[${requestId}] 📦 Operation: ${operation}`);
    logInfo(`[${requestId}] 📦 Variables keys: ${Object.keys(variables).join(', ')}`);
    logInfo(`[${requestId}] 📦 Data keys: ${Object.keys(data).join(', ')}`);

    // Handle different CopilotKit operations
    if (operation === 'availableAgents') {
      logInfo(`[${requestId}] 📋 Returning available agents`);
      return res.status(200).json({
        data: {
          availableAgents: [
            {
              id: 'game-agent',
              name: 'Game Agent',
              description: 'AI-powered game server management assistant',
              __typename: 'Agent'
            }
          ]
        }
      });
    }

    if (operation !== 'generateCopilotResponse') {
      logError(`[${requestId}] ❌ Unknown operation: ${operation}`);
      return res.status(400).json({ error: 'Unknown operation', requestId });
    }

    // Extract message from CopilotKit's messages array format
    const messages = data.messages || [];
    const threadId = data.threadId || `thread-${Date.now()}`;

    logInfo(`[${requestId}] 📦 Messages array length: ${messages.length}`);
    logInfo(`[${requestId}] 📦 ThreadId: ${threadId}`);

    // Convert CopilotKit messages to conversation history format
    const conversationHistory = messages
      .filter((msg: { textMessage?: { role?: string; content?: string } }) => msg.textMessage?.role && msg.textMessage?.content)
      .map((msg: { textMessage: { role: string; content: string } }) => ({
        role: msg.textMessage.role,
        content: msg.textMessage.content
      }));

    // Get the last user message
    const userMessages = messages.filter((msg: { textMessage?: { role?: string } }) => msg.textMessage?.role === 'user');
    const lastUserMessage = userMessages[userMessages.length - 1] as { textMessage?: { content?: string } } | undefined;
    const message = lastUserMessage?.textMessage?.content || '';

    logInfo(`[${requestId}] 📦 Conversation history: ${conversationHistory.length} messages`);
    logInfo(`[${requestId}] 📦 Found ${userMessages.length} user messages`);
    // User prompt content is PII-bearing — log only its length at info level;
    // the content itself is dev-only (logDebug).
    logInfo(`[${requestId}] 📥 Last user message: ${message.length} chars`);
    logDebug(`[${requestId}] 📥 Last user message content: ${message.substring(0, 100)}...`);

    if (!message) {
      logError(`[${requestId}] ❌ No user message found in messages array`);
      logDebug(`[${requestId}] 📦 Messages: ${JSON.stringify(messages).substring(0, 500)}`);
      return res.status(400).json({ error: 'No message provided', requestId });
    }

    // Get user identity for memory integration
    let userIdentity = await getUserIdentity();

    // Handle production Cognito identity (only if auth is NOT skipped)
    if (!!process.env.AGENTCORE_RUNTIME_ID && process.env.NEXT_PUBLIC_SKIP_AUTH !== 'true') {
      const cookies = parse(req.headers.cookie || '');
      const idToken = cookies.cognito_id_token;

      if (!idToken) {
        logError(`[${requestId}] ❌ Production requires Cognito authentication`);
        return res.status(401).json({
          error: 'Authentication required',
          message: 'Please sign in to continue',
          requestId
        });
      }

      // Reuse the ID-token claims verified earlier (signature + token_use +
      // binding to the access token) rather than raw-decoding again. If the
      // earlier verification didn't run/populate this, treat it as unauthenticated.
      const decoded = verifiedIdClaims;
      if (!decoded?.sub) {
        logError(`[${requestId}] ❌ ID token not verified`);
        return res.status(401).json({
          error: 'Invalid authentication',
          message: 'Please sign in again',
          requestId
        });
      }
      userIdentity = {
        userId: decoded.sub,           // Persistent Cognito user ID
        email: decoded.email,
        username: decoded.email || decoded.sub,
        displayName: decoded.email,
        authType: 'cognito'
      };
    }

    // Development: Ensure we have AWS credentials identity
    if (!process.env.AGENTCORE_RUNTIME_ID && !userIdentity) {
      logError(`[${requestId}] ❌ Development requires AWS credentials`);
      return res.status(500).json({
        error: 'AWS credentials not configured',
        message: 'Please configure AWS credentials for local development',
        requestId
      });
    }

    logInfo(`[${requestId}] ========================================`);
    logInfo(`[${requestId}] 🧠 MEMORY CONTEXT PREPARATION`);
    logInfo(`[${requestId}] ========================================`);
    // Identity fields are PII — redact at info level; full values dev-only.
    logInfo(`[${requestId}] 👤 User Identity:`);
    logInfo(`[${requestId}]   Type: ${userIdentity?.authType}`);
    logInfo(`[${requestId}]   User ID: ${redact(userIdentity?.userId)}`);
    logInfo(`[${requestId}]   Username: ${redact(userIdentity?.username)}`);
    logDebug(`[${requestId}]   Display: ${userIdentity?.displayName}`);
    logDebug(`[${requestId}]   Email: ${userIdentity?.email}`);

    // Environment-specific session isolation
    // Dev and prod use different session ID prefixes to prevent memory collision
    const isProduction = process.env.NODE_ENV === 'production';
    const envPrefix = isProduction ? 'prod' : 'dev';
    const isolatedThreadId = `${envPrefix}-${threadId}`;

    logInfo(`[${requestId}] 🔗 Thread/Session:`);
    logInfo(`[${requestId}]   Environment: ${envPrefix}`);
    logInfo(`[${requestId}]   Original Thread ID: ${threadId}`);
    logInfo(`[${requestId}]   Isolated Thread ID: ${isolatedThreadId}`);
    logInfo(`[${requestId}]   Memory isolation: Dev and prod sessions are separate`);

    // Call AgentCore Runtime
    let responseContent: string;

    if (isProduction) {
      // Production: Use AWS SDK to invoke AgentCore Runtime
      logInfo(`[${requestId}] 🚀 Calling AgentCore Runtime via AWS SDK`);
      logInfo(`[${requestId}] 📤 Sending prompt: ${message.length} chars`);
      logDebug(`[${requestId}] 📤 Prompt content: ${message.substring(0, 100)}...`);

      const runtimeId = process.env.AGENTCORE_RUNTIME_ID;
      if (!runtimeId) {
        logError(`[${requestId}] ❌ AGENTCORE_RUNTIME_ID not configured`);
        throw new Error('AGENTCORE_RUNTIME_ID not configured - check environment variables');
      }

      const accountId = await getAccountId();
      const region = process.env.AWS_REGION || 'us-west-2';

      logInfo(`[${requestId}] 🔧 AgentCore config: region=${region}, accountId=${accountId}, runtimeId=${runtimeId}`);

      const client = new BedrockAgentCoreClient({ region });

      // AgentCore Memory Pattern:
      // - Frontend sends ONLY current message + threadId
      // - AgentCore Memory automatically loads conversation history via runtimeSessionId
      // - DO NOT send conversation_history in payload (antipattern)
      const payload = {
        prompt: message,
        thread_id: isolatedThreadId,     // Environment-isolated session ID
        user_context: {
          user_id: userIdentity?.userId,   // Persistent user ID
          session_id: isolatedThreadId,    // Environment-isolated session ID
          email: userIdentity?.email,
          username: userIdentity?.username,
          display_name: userIdentity?.displayName,
          auth_type: userIdentity?.authType,
          account_id: userIdentity?.accountId,
          arn: userIdentity?.arn,
          ...userContext
        }
      };

      logInfo(`[${requestId}] 📦 Payload to AgentCore:`);
      logInfo(`[${requestId}]   prompt: ${message.substring(0, 50)}...`);
      logInfo(`[${requestId}]   thread_id: ${isolatedThreadId}`);
      logInfo(`[${requestId}]   user_context.user_id: ${redact(payload.user_context.user_id)}`);
      logInfo(`[${requestId}]   user_context.session_id: ${redact(payload.user_context.session_id)}`);
      logDebug(`[${requestId}]   user_context.display_name: ${payload.user_context.display_name}`);

      const payloadString = JSON.stringify(payload);
      const payloadBytes = new TextEncoder().encode(payloadString);

      const agentRuntimeArn = `arn:aws:bedrock-agentcore:${region}:${accountId}:runtime/${runtimeId}`;
      logInfo(`[${requestId}] 🔧 AgentCore Runtime ARN: ${agentRuntimeArn}`);

      // AgentCore Memory Integration:
      // - runtimeSessionId: Thread-based for conversation continuity within a chat
      // - runtimeUserId: User-based for long-term memory across sessions
      // - Environment prefix on sessionId prevents dev/prod memory collision
      const runtimeSessionIdValue = isolatedThreadId;  // Thread-based: dev-thread-123 or prod-thread-456
      const runtimeUserIdValue = userIdentity?.userId;  // User-based: aws-123-user or cognito-sub-abc

      // Production: Require authenticated user (no anonymous)
      if (isProduction && !runtimeUserIdValue) {
        logError(`[${requestId}] ❌ Production requires authenticated user`);
        throw new Error('Authentication required');
      }

      logInfo(`[${requestId}] 🧠 AgentCore Memory Parameters:`);
      logInfo(`[${requestId}]   runtimeSessionId: ${redact(runtimeSessionIdValue)} (STM: conversation history)`);
      logInfo(`[${requestId}]   runtimeUserId: ${redact(runtimeUserIdValue)} (LTM: user preferences)`);

      const command = new InvokeAgentRuntimeCommand({
        agentRuntimeArn,
        contentType: 'application/json',
        payload: payloadBytes,
        runtimeSessionId: runtimeSessionIdValue,        // STM: Session-scoped conversation history
        runtimeUserId: runtimeUserIdValue               // LTM: User-scoped preferences & context
      });

      const sdkResponse = await client.send(command);

      logInfo(`[${requestId}] ✅ AgentCore SDK call completed`);

      // The response has a streaming response property
      if (sdkResponse.response) {
        const chunks: Uint8Array[] = [];
        const stream = sdkResponse.response as AsyncIterable<Uint8Array>;
        for await (const chunk of stream) {
          chunks.push(chunk);
        }
        const responseBytes = Buffer.concat(chunks);
        responseContent = responseBytes.toString('utf-8');

        logInfo(`[${requestId}] 📦 STAGE 1 - RAW SDK STREAM`);
        logInfo(`[${requestId}] Type: ${typeof responseContent}`);
        logDebug(`[${requestId}] First 200 chars: ${responseContent.substring(0, 200)}`);
        logInfo(`[${requestId}] Has escaped quotes: ${responseContent.includes('\\"')}`);
        logInfo(`[${requestId}] Has escaped newlines: ${responseContent.includes('\\n')}`);

        // AgentCore Runtime automatically JSON-serializes return values
        // According to AWS docs, AgentCore wraps string returns in JSON
        try {
          const parsed = JSON.parse(responseContent);
          if (typeof parsed === 'string') {
            responseContent = parsed;
            logInfo(`[${requestId}] 📥 STAGE 2 - PARSED AGENTCORE JSON WRAPPER`);
            logInfo(`[${requestId}] Extracted string length: ${responseContent.length}`);
            logInfo(`[${requestId}] Has escaped newlines: ${responseContent.includes('\\n')}`);
          } else {
            logInfo(`[${requestId}] 📥 STAGE 2 - UNEXPECTED JSON TYPE: ${typeof parsed}`);
            responseContent = String(parsed);
          }
        } catch {
          logInfo(`[${requestId}] 📥 STAGE 2 - RAW RESPONSE (no JSON wrapper)`);
          // Use as-is if not JSON-wrapped
        }

        logInfo(`[${requestId}] 📥 Response length: ${responseContent.length}`);
      } else {
        logError(`[${requestId}] ❌ No response stream in AgentCore response`);
        responseContent = 'No response stream from AgentCore Runtime';
      }

      logInfo(`[${requestId}] ✅ AgentCore responded via SDK`);
    } else {
      // Fallback: Use local HTTP endpoint (only when AGENTCORE_RUNTIME_ID not set)
      const backendUrl = process.env.BACKEND_URL || 'http://localhost:8080';

      logInfo(`[${requestId}] 🚀 Calling local AgentCore at ${backendUrl}/invocations`);
      logInfo(`[${requestId}] 📤 Sending prompt: ${message.length} chars`);
      logDebug(`[${requestId}] 📤 Prompt content: ${message.substring(0, 100)}...`);

      const response = await fetch(`${backendUrl}/invocations`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Request-ID': requestId,
        },
        body: JSON.stringify({
          prompt: message,
          thread_id: threadId,              // Session-level ID (CopilotKit)
          user_context: {
            user_id: userIdentity?.userId,   // Persistent user ID
            session_id: threadId,           // Session ID
            email: userIdentity?.email,
            username: userIdentity?.username,
            display_name: userIdentity?.displayName,
            auth_type: userIdentity?.authType,
            account_id: userIdentity?.accountId,
            arn: userIdentity?.arn,
            ...userContext
          }
        }),
      });

      logInfo(`[${requestId}] ✅ Local AgentCore responded with status: ${response.status}`);

      const backendData = await response.json();

      logInfo(`[${requestId}] 📦 STAGE 1 - RAW BACKEND RESPONSE`);
      logInfo(`[${requestId}] Type: ${typeof backendData}`);
      logDebug(`[${requestId}] Value: ${JSON.stringify(backendData).substring(0, 200)}`);

      // Ensure we always get a string
      if (typeof backendData === 'string') {
        responseContent = backendData;
      } else if (backendData.response) {
        // If response field exists, convert it to string
        responseContent = typeof backendData.response === 'string'
          ? backendData.response
          : JSON.stringify(backendData.response);
      } else {
        // Fallback: stringify the whole object
        responseContent = JSON.stringify(backendData);
      }

      logInfo(`[${requestId}] 📦 STAGE 2 - AFTER EXTRACTION`);
      logInfo(`[${requestId}] Type: ${typeof responseContent}`);
      logDebug(`[${requestId}] First 200 chars: ${responseContent.substring(0, 200)}`);
      logInfo(`[${requestId}] Has escaped quotes: ${responseContent.includes('\\"')}`);
      logInfo(`[${requestId}] Has escaped newlines: ${responseContent.includes('\\n')}`);
    }

    logInfo(`[${requestId}] 📦 STAGE 3 - BEFORE COPILOTKIT FORMATTING`);
    logInfo(`[${requestId}] Type: ${typeof responseContent}`);
    logInfo(`[${requestId}] Length: ${responseContent.length}`);
    logDebug(`[${requestId}] First 200 chars: ${responseContent.substring(0, 200)}`);
    logInfo(`[${requestId}] Has escaped quotes: ${responseContent.includes('\\"')}`);
    logInfo(`[${requestId}] Has escaped newlines: ${responseContent.includes('\\n')}`);

    // Final cleanup: handle any remaining escaped characters
    // This should rarely be needed after proper JSON parsing, but provides safety
    if (responseContent.includes('\\n')) {
      responseContent = responseContent.replace(/\\n/g, '\n');
      logInfo(`[${requestId}] 📥 STAGE 3.5 - UNESCAPED REMAINING NEWLINES`);
    }
    if (responseContent.includes('\\"')) {
      responseContent = responseContent.replace(/\\"/g, '"');
      logInfo(`[${requestId}] 📥 STAGE 3.5 - UNESCAPED REMAINING QUOTES`);
    }

    logInfo(`[${requestId}] 🔄 Formatting response for CopilotKit`);

    // CopilotKit expects content as an array of strings (it calls .join() on it)
    const copilotResponse = {
      data: {
        generateCopilotResponse: {
          threadId: threadId,
          runId: `run-${Date.now()}`,
          messages: [
            {
              id: `msg-${Date.now()}`,
              createdAt: new Date().toISOString(),
              content: [responseContent],  // Array of strings
              role: 'assistant',
              status: { code: 'Success' },
              __typename: 'TextMessageOutput'
            }
          ],
          __typename: 'CopilotResponse'
        }
      }
    };

    logInfo(`[${requestId}] 📦 STAGE 4 - COPILOTKIT RESPONSE`);
    logInfo(`[${requestId}] Content array length: ${copilotResponse.data.generateCopilotResponse.messages[0].content.length}`);
    logInfo(`[${requestId}] Content[0] type: ${typeof copilotResponse.data.generateCopilotResponse.messages[0].content[0]}`);
    logDebug(`[${requestId}] Content[0] first 200 chars: ${copilotResponse.data.generateCopilotResponse.messages[0].content[0].substring(0, 200)}`);

    logInfo(`[${requestId}] 📤 Sending CopilotKit formatted response`);
    logInfo(`[${requestId}] ========================================`);
    return res.status(200).json(copilotResponse);

  } catch (error) {
    // Specific handling for IAM permission errors
    if (error instanceof Error && error.name === 'AccessDeniedException') {
      logError(`[${requestId}] ❌ IAM Permission Error:`, error);

      // Check if it's the specific InvokeAgentRuntimeForUser permission issue
      if (error.message.includes('InvokeAgentRuntimeForUser')) {
        logError(`[${requestId}] 💡 Missing IAM permission: bedrock-agentcore:InvokeAgentRuntimeForUser`);
        logError(`[${requestId}] 💡 This permission is required when using runtimeUserId for memory features`);
        logError(`[${requestId}] 💡 Fix: Add bedrock-agentcore:InvokeAgentRuntimeForUser to ECS task role`);

        return res.status(500).json({
          error: 'Memory feature configuration error',
          message: 'The AI assistant is experiencing a configuration issue with memory features. Please contact support.',
          details: process.env.NODE_ENV === 'development' ? error.message : undefined,
          requestId
        });
      }

      // Generic IAM error
      return res.status(403).json({
        error: 'Authorization error',
        message: 'The AI assistant does not have permission to process this request.',
        requestId
      });
    }

    // Generic error handling
    logError(`[${requestId}] ❌ Error processing request:`, error instanceof Error ? error : new Error(String(error)));
    return res.status(500).json({
      error: 'Internal server error',
      details: error instanceof Error ? error.message : String(error),
      requestId
    });
  }
}
