#!/usr/bin/env python3
"""
AgentCore Runtime entrypoint.

Security features:
- Input validation and sanitization
- Encryption context for data protection
- Rate limiting support
- Audit logging

Performance Optimization:
- Container pre-warming at startup
- Singleton model and MCP client initialization
- Reduces first-request latency by 2-4 seconds
"""

# Standard library
import os
import sys
import time
import traceback

# Third-party packages
import boto3

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

# Third-party packages
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore[import-untyped]

# Local modules
from agents.cost_report_scope import (
    ScopeAuthorizationError,
    build_request_scope,
    reset_request_scope,
    resolve_scope_actor,
    set_request_scope,
)
from agents.orchestrator import run_orchestrator
from config.settings import (
    ALLOW_LOCAL_IDENTITY_BYPASS,
    AWS_REGION,
    BOTO3_CLIENT_CONFIG,
    COGNITO_CLIENT_ID,
    COGNITO_ISSUER,
    COST_SNAPSHOT_STORE_REQUIRED,
    COST_SNAPSHOT_TABLE_NAME,
    DEPLOYMENT_TENANT_ID,
    DEPLOYMENT_WORKSPACE_ID,
    HOSTED_RUNTIME,
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    USE_BEDROCK_SESSIONS,
)
from runtime_identity import RuntimeIdentityError, verify_cognito_runtime_identity
from utils.logger import logger
from utils.response_parser import ResponseParser
from utils.security import (
    InputValidationError,
    RateLimitExceeded,
    SecurityViolationError,
    check_rate_limit,
    create_encryption_context,
    get_rate_limit_key,
    sanitize_log_data,
    validate_conversation_history,
    validate_prompt,
    validate_user_context,
    verify_request_authorization,
)


def validate_aws_credentials():
    """
    Validate AWS credentials at startup.

    Returns:
        bool: True if credentials are valid, False otherwise
    """
    try:
        # Third-party packages
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        sts = boto3.client("sts", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        identity = sts.get_caller_identity()
        logger.info(f"✅ AWS credentials verified (Account: {identity['Account']})")
        return True
    except (BotoCoreError, ClientError, NoCredentialsError) as e:
        logger.error(f"❌ AWS credentials failed: {e}")
        logger.warning("⚠️ Continuing without verified credentials - AWS operations will fail")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error during credential check: {e}")
        logger.warning("⚠️ Continuing without verified credentials - AWS operations will fail")
        return False


def prewarm_container():
    """
    Pre-warm container by initializing expensive resources at startup.

    This reduces first-request latency by 2-4 seconds by initializing:
    - Bedrock model singleton
    - MCP clients for all known servers
    - Knowledge Base retrieval tools

    Called at module load time (container startup).
    """
    start_time = time.perf_counter()
    logger.info("🔥 Pre-warming container...")

    try:
        # 1. Pre-warm Bedrock model singleton
        logger.debug("  Initializing Bedrock model...")
        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        create_cached_bedrock_model()

        # 2. Pre-warm MCP clients for known servers
        logger.debug("  Initializing MCP clients...")
        # Local modules
        from utils.mcp_client_factory import create_mcp_client

        mcp_servers = ["aws-api-mcp-server", "eks-mcp-server", "billing-cost-management-mcp-server"]
        for server in mcp_servers:
            try:
                create_mcp_client(server)
            except Exception as e:
                logger.debug(f"  MCP client {server} init skipped: {e}")

        # 3. Pre-warm Knowledge Base tools (if configured)
        logger.debug("  Initializing KB tools...")
        # Local modules
        from config.settings import COST_KB_ID, EKS_KB_ID, GAMELIFT_KB_ID
        from utils.kb_tools import create_kb_retrieve_tool

        for kb_id in [GAMELIFT_KB_ID, EKS_KB_ID, COST_KB_ID]:
            if kb_id:
                try:
                    create_kb_retrieve_tool(kb_id, AWS_REGION)
                except Exception as e:
                    logger.debug(f"  KB tool init skipped: {e}")

        elapsed = time.perf_counter() - start_time
        logger.info(f"✅ Container pre-warmed in {elapsed:.2f}s")

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.warning(f"⚠️ Pre-warming partially failed ({elapsed:.2f}s): {e}")


# Validate credentials at module load. Keep the result as a readiness signal
# instead of discarding it: a credential failure here means AWS calls will fail,
# so the entrypoint can return a clear "initializing/unavailable" message rather
# than an opaque downstream error on the first real request (#123). We do NOT
# hard-fail startup — that would crash-loop the container on a transient STS
# blip, and IAM role creds typically resolve momentarily after start.
_CREDENTIALS_OK = validate_aws_credentials()

# Pre-warm container at startup
prewarm_container()

app = BedrockAgentCoreApp()

# Memory ID from environment (set by AgentCore CLI or deployment)
MEMORY_ID = os.getenv("BEDROCK_AGENTCORE_MEMORY_ID")

# Fallback: Read from .bedrock_agentcore.yaml if env var not set
if not MEMORY_ID:
    try:
        # Standard library
        import pathlib

        config_file = pathlib.Path(__file__).parent.parent / ".bedrock_agentcore.yaml"
        if config_file.exists():
            with open(config_file, "r") as f:
                for line in f:
                    if "memory_id:" in line:
                        MEMORY_ID = line.split("memory_id:")[1].strip()
                        logger.info(f"📋 Memory ID loaded from config file: {MEMORY_ID}")
                        break
    except Exception as e:
        logger.warning(f"⚠️ Could not read memory ID from config: {e}")

if USE_BEDROCK_SESSIONS and MEMORY_ID:
    logger.info(f"🧠 AgentCore Memory enabled: {MEMORY_ID}")
elif USE_BEDROCK_SESSIONS:
    logger.warning("⚠️ BEDROCK_AGENTCORE_MEMORY_ID not set - memory disabled")
else:
    logger.info("ℹ️  Bedrock Sessions disabled")


@app.entrypoint
def invoke_agent(prompt, context=None):
    """
    AgentCore Runtime entrypoint.

    Security features:
    - Input validation and sanitization
    - User context validation
    - Audit logging with sensitive data redaction

    Args:
        prompt: Can be a string or dict with 'prompt' key
        context: AgentCore RequestContext object (optional)

    Returns:
        Agent response string
    """
    try:
        logger.info("=" * 80)
        logger.info("🚀 AGENTCORE INVOCATION START")
        logger.info("=" * 80)

        # Readiness gate (#123): if credentials failed at startup, re-check once
        # here (they may have resolved post-start) and return a clear message
        # rather than letting the request fail opaquely deep in an AWS call.
        global _CREDENTIALS_OK
        if not _CREDENTIALS_OK:
            _CREDENTIALS_OK = validate_aws_credentials()
            if not _CREDENTIALS_OK:
                logger.error("❌ Service not ready: AWS credentials unavailable")
                return "The service is still initializing and can't reach AWS yet. Please retry in a few moments."

        # The hosted runtime is configured for Cognito JWT bearer authorization.
        # AgentCore forwards the validated Authorization header to agent code;
        # application code independently verifies it before admitting claims.
        verified_runtime_identity = None

        # DIAGNOSTIC: Check AgentCore context object. Never log token contents or
        # raw identity values.
        logger.info("🔍 AgentCore Context Inspection:")
        if context:
            logger.info(f"   Context type: {type(context)}")
            if hasattr(context, "session_id"):
                logger.info(f"   context.session_id: {context.session_id}")
        else:
            logger.info("   Context is None")

        # Extract enhanced user context
        if isinstance(prompt, dict):
            user_prompt = prompt.get("prompt", "")
            thread_id = prompt.get("thread_id")
            user_context = prompt.get("user_context", {})
            logger.info("📦 Prompt type: dict")
            logger.info(f"📦 Prompt keys: {list(prompt.keys())}")
        else:
            user_prompt = str(prompt)
            thread_id = None
            user_context = {}
            logger.info(f"📦 Prompt type: {type(prompt)}")

        # Security: Validate and sanitize input
        logger.info("🔒 Security: Validating input...")
        try:
            user_prompt = validate_prompt(user_prompt, strict_mode=False)
            user_context = validate_user_context(user_context)
            logger.info(f"✅ Input validation passed (prompt length: {len(user_prompt)})")
        except InputValidationError as e:
            logger.error(f"❌ Input validation failed: {e}")
            return f"I'm sorry, but I couldn't process your request: {e}"
        except SecurityViolationError as e:
            logger.error(f"🚨 Security violation detected: {e}")
            return "I'm sorry, but your request could not be processed due to security restrictions."

        shared_report_mode = COST_SNAPSHOT_STORE_REQUIRED or bool(COST_SNAPSHOT_TABLE_NAME)
        local_identity_bypass = ALLOW_LOCAL_IDENTITY_BYPASS and not HOSTED_RUNTIME
        if not local_identity_bypass:
            try:
                verified_runtime_identity = verify_cognito_runtime_identity(
                    context,
                    issuer=COGNITO_ISSUER,
                    client_id=COGNITO_CLIENT_ID,
                )
            except RuntimeIdentityError:
                logger.warning("⚠️ Rejected request: Cognito runtime identity verification failed")
                return "I'm sorry, but your request could not be processed due to an identity verification issue."

            if not verified_runtime_identity.groups.intersection({"admin", "users"}):
                logger.warning("⚠️ Rejected request: verified user is not in an approved group")
                return "I'm sorry, but your request could not be processed due to an identity verification issue."

        # Security: Log sanitized request info (redact sensitive data)
        logger.info(f"📝 User prompt (sanitized): '{sanitize_log_data(user_prompt, 100)}'")
        logger.info(f"🔗 Thread ID: {thread_id}")

        # Hosted identity comes only from the cryptographically verified access
        # token. Local development keeps the existing body identity fallback
        # because it has no hosted shared store or JWT authorizer.
        persistent_user_id = (
            verified_runtime_identity.subject_id
            if verified_runtime_identity is not None
            else user_context.get("user_id", "anonymous")
        )
        username = user_context.get("username", "user")
        display_name = user_context.get("display_name", username)
        auth_type = "cognito" if verified_runtime_identity is not None else user_context.get("auth_type", "unknown")
        actor_id = persistent_user_id

        # Rate limiting: per-user throttle (WA GenAI Lens: Operational Excellence 2.2)
        try:
            rl_key = get_rate_limit_key(persistent_user_id, "invoke_agent")
            check_rate_limit(rl_key, RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)
        except RateLimitExceeded as e:
            logger.warning(f"⚠️ Rate limit hit for user {persistent_user_id}: {e}")
            return str(e)

        # Use session_id from frontend (environment-isolated: dev-{threadId} or prod-{threadId})
        # This prevents dev/prod memory collision while maintaining conversation continuity
        session_id = user_context.get("session_id") or thread_id or "default"

        logger.info(f"👤 Authenticated user context [{auth_type}]")
        logger.info(f"🆔 Actor ID: {actor_id}")
        logger.info(f"📍 Session ID: {session_id} (environment-isolated)")
        logger.info(f"🔑 Persistent User ID: {persistent_user_id}")

        verified_groups = (
            sorted(verified_runtime_identity.groups)
            if verified_runtime_identity is not None
            else user_context.get("groups", [])
        )
        verified_scopes = (
            sorted(verified_runtime_identity.scopes)
            if verified_runtime_identity is not None
            else user_context.get("scopes", [])
        )
        verified_client_id = (
            verified_runtime_identity.client_id
            if verified_runtime_identity is not None
            else user_context.get("client_id")
        )

        # Create enhanced agent context with rich user info. Hosted authority
        # fields are reconstructed from the verified JWT and deployment bindings;
        # body claims cannot elevate or replace them.
        agent_context = {
            "user_id": persistent_user_id,
            "client_id": verified_client_id,
            "audience": verified_client_id if verified_runtime_identity is not None else user_context.get("audience"),
            "groups": verified_groups,
            "scopes": verified_scopes,
            "tenant": DEPLOYMENT_TENANT_ID if verified_runtime_identity is not None else user_context.get("tenant"),
            "workspace": (
                DEPLOYMENT_WORKSPACE_ID if verified_runtime_identity is not None else user_context.get("workspace")
            ),
            "session_id": session_id,
            "thread_id": thread_id,
            "username": username,
            "display_name": display_name,
            "auth_type": auth_type,
            "user_info": user_context,
            "actor_id": actor_id,
        }

        logger.info(f"🧠 Memory Configuration:")
        logger.info(f"   USE_BEDROCK_SESSIONS: {USE_BEDROCK_SESSIONS}")
        logger.info(f"   MEMORY_ID: {MEMORY_ID}")
        logger.info(f"   Actor ID: {actor_id}")
        logger.info(f"   Session ID: {session_id}")

        # Run the orchestrator inline. Timeouts are enforced INSIDE the agent loop
        # by WallClockTimeoutHook (orchestrator 150s, specialists 90s), which
        # cancels the next tool call cleanly — emitting a paired toolResult so the
        # turn is never left half-written. The previous ThreadPoolExecutor +
        # future.result(timeout) ABANDONED the worker thread on timeout: it kept
        # running mid-tool-call and could persist a toolUse with no toolResult,
        # permanently poisoning the session (#155 / #125). The in-loop hooks fire
        # before the old 180s outer budget would have, so no coverage is lost.
        logger.info("🎯 Calling run_orchestrator (in-loop wall-clock hooks enforce timeout)...")
        # Bind cost-report reuse to the trusted deployment tenant/workspace plus
        # the request actor (#365). Set at the AgentCore boundary via a ContextVar
        # so nested specialist tool calls inherit it, and reset afterwards so the
        # scope never leaks across requests handled by the same worker.
        #
        # In shared mode the actor is taken only from the independently verified
        # Cognito access token. A conflicting body identity is rejected as an
        # identity-confusion signal. Raw identifiers are never logged.
        try:
            report_actor = resolve_scope_actor(
                trusted_actor=(verified_runtime_identity.subject_id if verified_runtime_identity is not None else None),
                body_user_id=user_context.get("user_id"),
                shared_mode=shared_report_mode,
            )
        except ScopeAuthorizationError:
            # Do not echo the offending identifiers; the message is generic.
            logger.warning("⚠️ Rejected request: could not establish a trusted cost-report actor")
            return "I'm sorry, but your request could not be processed due to an identity verification issue."

        report_scope = build_request_scope(
            report_actor,
            tenant=DEPLOYMENT_TENANT_ID,
            workspace=DEPLOYMENT_WORKSPACE_ID,
        )
        scope_token = set_request_scope(report_scope)
        try:
            response = run_orchestrator(query=user_prompt, context=agent_context)
        finally:
            reset_request_scope(scope_token)

        logger.info(f"✅ Orchestrator returned response")
        logger.info(f"   Response type: {type(response)}")

        response_text = ResponseParser.parse(response)
        logger.info(f"📤 Response processed: {len(response_text)} chars")
        logger.info("=" * 80)
        logger.info("✅ AGENTCORE INVOCATION COMPLETE")
        logger.info("=" * 80)

        return response_text

    except Exception as e:
        error_msg = f"❌ AGENTCORE INVOCATION ERROR: {e}"
        traceback_msg = f"Traceback: {traceback.format_exc()}"

        # Log to logger (captured by CloudWatch via loguru)
        logger.error("=" * 80)
        logger.error(error_msg)
        logger.error(traceback_msg)
        logger.error("=" * 80)

        return (
            "I encountered an error processing your request. Please try again or contact support if the issue persists."
        )


# Configure for local development
if __name__ == "__main__":
    logger.info("🤖 Starting AgentCore Runtime...")
    logger.info("🔧 Environment: development")
    logger.info("🌐 Server will be available at: http://localhost:8080")
    logger.info(
        "💡 Test with: curl -X POST http://localhost:8080/invocations -H 'Content-Type: application/json' -d '{\"prompt\": \"Hello\"}'"
    )

    # MCP connections handled by MCP Server Pool
    logger.info("🚀 MCP connections will use MCP Server Pool")

    app.run(host="0.0.0.0", port=8080)
