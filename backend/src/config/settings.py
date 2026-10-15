"""
Configuration settings for the backend.

This module loads environment variables from the .env.local file in the UI directory
and provides configuration settings for the backend.
"""

# Standard library
import os
import pathlib
from typing import Literal, TypedDict, cast

# Third-party packages
from dotenv import load_dotenv
from loguru import logger

# Local modules
from config.model_settings import resolve_model_ids

# Get the project root directory
PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent.parent
UI_DIR = PROJECT_ROOT / "ui"
ENV_FILE = UI_DIR / ".env.local"

# Load environment variables from .env.local in the UI directory (if running locally)
if ENV_FILE.exists():
    logger.info(f"Loading environment variables from {ENV_FILE}")
    load_dotenv(ENV_FILE)
else:
    # In containerized environments, we expect environment variables to be passed directly
    logger.info("Using system environment variables (containerized environment)")

# AWS settings
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")  # Default to us-west-2
AWS_PROFILE = os.getenv("AWS_PROFILE")
# Note: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are read directly from environment
# by boto3 and MCP servers. In production, IAM roles are used automatically.
# In development, credentials can be set via environment variables or AWS profiles.

# Set AWS profile in environment if specified
if AWS_PROFILE:
    os.environ["AWS_PROFILE"] = AWS_PROFILE
    logger.info(f"Using AWS Profile: {AWS_PROFILE}")

# Bedrock role settings - global profiles provide cross-region routing.
# Canonical role variables override legacy compatibility aliases; see
# config.model_settings.resolve_model_ids for deterministic precedence.
ORCHESTRATOR_MODEL_ID, SPECIALIST_MODEL_ID = resolve_model_ids()

# Python import compatibility for existing integrations. New code should use
# the role-based names above; these aliases do not imply failover order.
BEDROCK_MODEL_ID = ORCHESTRATOR_MODEL_ID
BEDROCK_MODEL_ID_SECONDARY = SPECIALIST_MODEL_ID

# Bedrock Guardrails - Production security
BEDROCK_GUARDRAIL_ID = os.getenv("GBAW_BEDROCK_GUARDRAIL_ID")
BEDROCK_GUARDRAIL_VERSION = os.getenv("GBAW_BEDROCK_GUARDRAIL_VERSION", "DRAFT")
BEDROCK_GUARDRAIL_ENABLED = os.getenv("GBAW_BEDROCK_GUARDRAIL_ENABLED", "true").lower() == "true"

# Logging settings
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", PROJECT_ROOT / "logs" / "backend.log")


# Memory settings
# Memory is enabled by default in AgentCore Runtime environments
USE_BEDROCK_SESSIONS = os.getenv("GBAW_USE_BEDROCK_SESSIONS", "true").lower() == "true"

# Memory ID (auto-set by AgentCore CLI during deployment)
BEDROCK_AGENTCORE_MEMORY_ID = os.getenv("BEDROCK_AGENTCORE_MEMORY_ID")

# Knowledge Base settings - Multi-KB architecture
# Each specialist agent has its own dedicated KB for better retrieval precision
GAMELIFT_KB_ID = os.getenv("GBAW_GAMELIFT_KB_ID")
EKS_KB_ID = os.getenv("GBAW_EKS_KB_ID")
COST_KB_ID = os.getenv("GBAW_COST_KB_ID")

# Legacy support: KNOWLEDGE_BASE_ID falls back to GAMELIFT_KB_ID
KNOWLEDGE_BASE_ID = os.getenv("GBAW_KNOWLEDGE_BASE_ID") or GAMELIFT_KB_ID

# Cost report shared snapshot store (#365)
# Validated cost report snapshots are persisted so a report ID created by one
# AgentCore worker resolves from any worker for the configured TTL. When the
# DynamoDB table name is set (wired by deploy.sh from CloudFormation), the shared
# encrypted store is used; otherwise the runtime falls back to an in-memory TTL
# cache — but only when the shared store is not required (see below).
COST_SNAPSHOT_TABLE_NAME = os.getenv("GBAW_COST_SNAPSHOT_TABLE_NAME") or None

# Explicit shared-store-required switch. Hosted deployments set this to "true"
# (wired by deploy.sh / Deploy-GameAgent) so a missing table name fails closed
# instead of silently degrading to a process-local in-memory cache that cannot
# satisfy cross-worker reuse. Local development and tests leave it false, which
# permits the in-memory fallback.
COST_SNAPSHOT_STORE_REQUIRED = os.getenv("GBAW_COST_SNAPSHOT_REQUIRED", "false").lower() == "true"


def _coerce_ttl_seconds(raw: str | None, *, default: int, minimum: int, maximum: int) -> int:
    """Return a bounded, positive TTL in seconds.

    A non-integer, non-positive, or out-of-range value falls back to ``default``
    with a warning rather than crashing the runtime, so a malformed deployment
    variable cannot take the whole service down. Deploy-time validation
    (deploy.sh / Deploy-GameAgent) rejects bad values earlier; this is the
    defensive runtime floor/ceiling.
    """
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"Invalid GBAW_COST_SNAPSHOT_TTL_SECONDS={raw!r}; using default {default}")
        return default
    if value < minimum or value > maximum:
        logger.warning(
            f"GBAW_COST_SNAPSHOT_TTL_SECONDS={value} out of bounds [{minimum}, {maximum}]; using default {default}"
        )
        return default
    return value


# Bounded positive TTL. Minimum keeps a follow-up window usable; maximum caps how
# long a validated snapshot may be reused before a fresh Cost Explorer query is
# required (open-period billing data can be revised by AWS).
COST_SNAPSHOT_TTL_MIN_SECONDS = 60
COST_SNAPSHOT_TTL_MAX_SECONDS = 86_400
COST_SNAPSHOT_TTL_SECONDS = _coerce_ttl_seconds(
    os.getenv("GBAW_COST_SNAPSHOT_TTL_SECONDS"),
    default=1800,
    minimum=COST_SNAPSHOT_TTL_MIN_SECONDS,
    maximum=COST_SNAPSHOT_TTL_MAX_SECONDS,
)

# Trusted deployment identity binding (#365)
# Report reuse is scoped to the trusted deployment tenant/workspace plus the
# request actor so a report ID cannot cross an authorization boundary. These
# values are resolved at deploy time (config/load_deployment_settings.py) and
# passed into the runtime environment; they default to the same deployment
# defaults used elsewhere so local runs remain deterministic.
DEPLOYMENT_TENANT_ID = os.getenv("GBAW_TENANT_ID", "").strip() or "default-tenant"
DEPLOYMENT_WORKSPACE_ID = os.getenv("GBAW_WORKSPACE_ID", "").strip() or "default-workspace"
COGNITO_ISSUER = os.getenv("GBAW_COGNITO_ISSUER", "").strip().rstrip("/")
COGNITO_CLIENT_ID = os.getenv("GBAW_COGNITO_CLIENT_ID", "").strip()
HOSTED_RUNTIME = os.getenv("GBAW_HOSTED_RUNTIME", "false").lower() == "true"
ALLOW_LOCAL_IDENTITY_BYPASS = os.getenv("GBAW_ALLOW_LOCAL_IDENTITY_BYPASS", "false").lower() == "true"

# =============================================================================
# RUNTIME LISTENER BIND (#470)
# =============================================================================
# The AgentCore application listens on loopback (127.0.0.1) for every local
# run so an unauthenticated developer backend is never reachable from other
# machines. The all-interface bind (0.0.0.0) is correct ONLY inside the hosted
# AgentCore container, whose unpublished network plus per-request JWT verifier
# form the boundary — there the in-container bind must accept the platform's
# own routing.
#
# Local development also bypasses identity verification
# (GBAW_ALLOW_LOCAL_IDENTITY_BYPASS=true, set by scripts/dev/start.sh). An
# unauthenticated backend reachable beyond the host would let any machine that
# can reach it invoke agents using the developer's live AWS credentials.
#
# An environment flag cannot, by itself, prove that a real AgentCore JWT
# boundary is in front of the process. So the two dangerous inputs are not
# allowed to combine: hosted mode grants the all-interface bind only when the
# local identity bypass is off. hosted_runtime=true together with
# allow_local_identity_bypass=true fails closed. This is defense-in-depth:
# invoke_agent already ignores the bypass whenever HOSTED_RUNTIME is true
# (ALLOW_LOCAL_IDENTITY_BYPASS and not HOSTED_RUNTIME), so the JWT verifier
# runs on every request in hosted mode regardless of the bypass flag. Refusing
# the combination at startup prevents a contradictory, drifted configuration
# from taking effect silently rather than being the sole barrier to exposure.
RUNTIME_LOOPBACK_HOST = "127.0.0.1"
RUNTIME_ALL_INTERFACES_HOST = "0.0.0.0"  # nosec B104 - hosted container bind only; see resolve_runtime_host
# AgentCore serves on the fixed platform port 8080 (see scripts/dev/start.sh,
# scripts/deploy.sh, and the AgentCore SDK default). It is intentionally not
# configurable.
RUNTIME_PORT = 8080


class RuntimeBindError(RuntimeError):
    """Raised when the requested listener bind is unsafe for the current mode.

    Specifically, when the hosted all-interface bind is requested while the
    local identity bypass is active. This is a defense-in-depth / config-drift
    guard: ``invoke_agent`` already ignores the bypass whenever
    ``HOSTED_RUNTIME`` is true (it computes
    ``ALLOW_LOCAL_IDENTITY_BYPASS and not HOSTED_RUNTIME``), so the per-request
    JWT verifier runs regardless. Refusing this combination at startup keeps a
    contradictory configuration from silently taking effect rather than being
    the only thing standing between the bypass and an exposed backend.
    """


def resolve_runtime_host(
    *,
    hosted_runtime: bool,
    allow_local_identity_bypass: bool,
) -> str:
    """Resolve the listener host for an AgentCore entrypoint.

    Rules (see the module comment above and issue #470):

    - Local execution always binds loopback (``127.0.0.1``).
    - Hosted AgentCore (``hosted_runtime=True``) binds all interfaces
      (``0.0.0.0``) so the AWS-managed container network can route to it — but
      only when the local identity bypass is off. The unpublished container
      network and per-request JWT verifier are the boundary.
    - ``hosted_runtime=True`` combined with ``allow_local_identity_bypass=True``
      fails closed (raises ``RuntimeBindError``). This is defense-in-depth
      against config drift: ``invoke_agent`` already disregards the bypass while
      hosted (``ALLOW_LOCAL_IDENTITY_BYPASS and not HOSTED_RUNTIME``), so the
      JWT verifier runs regardless. Refusing the contradictory combination at
      startup keeps it from taking effect silently.

    Returns the host string to pass to ``app.run(host=...)``.
    """
    if not hosted_runtime:
        return RUNTIME_LOOPBACK_HOST

    if allow_local_identity_bypass:
        raise RuntimeBindError(
            "Refusing to start: GBAW_HOSTED_RUNTIME=true selects the "
            "all-interface (0.0.0.0) container bind, but "
            "GBAW_ALLOW_LOCAL_IDENTITY_BYPASS is also true. These settings "
            "contradict each other: hosted mode already ignores the bypass and "
            "verifies a Cognito JWT per request, so this combination indicates "
            "configuration drift rather than an intended state. Unset the "
            "bypass in the hosted container, or leave GBAW_HOSTED_RUNTIME unset "
            "for local development (binds 127.0.0.1)."
        )

    return RUNTIME_ALL_INTERFACES_HOST


# Memory layer configuration
MEMORY_SESSION_TTL_HOURS = int(os.getenv("GBAW_MEMORY_SESSION_TTL_HOURS", "24"))  # Conversation memory
MEMORY_USER_TTL_DAYS = int(os.getenv("GBAW_MEMORY_USER_TTL_DAYS", "30"))  # User memory
MEMORY_LONG_TERM_ENABLED = os.getenv("GBAW_MEMORY_LONG_TERM_ENABLED", "true").lower() == "true"  # Enable LTM by default
MEMORY_REQUIRED = os.getenv("GBAW_MEMORY_REQUIRED", "false").lower() == "true"  # Hard fail if memory unavailable

# Agent loop stopping conditions (Well-Architected GenAI Lens: Cost 3.5, Reliability 5.3)
# max_turns: Maximum reasoning/tool-call cycles before the agent is forced to stop.
# Prevents runaway loops and bounds per-request cost.
AGENT_MAX_TURNS_ORCHESTRATOR = int(os.getenv("GBAW_AGENT_MAX_TURNS_ORCHESTRATOR", "15"))
AGENT_MAX_TURNS_SPECIALIST = int(os.getenv("GBAW_AGENT_MAX_TURNS_SPECIALIST", "10"))

# Wall-clock timeouts (Guardian Security: "Insufficient timeout configurations")
# Hard ceiling on elapsed time for agent execution. Catches hung Bedrock calls,
# stuck MCP servers, or slow multi-step reasoning that max_turns alone won't stop.
# The invoke_agent entrypoint enforces AGENT_TIMEOUT_REQUEST as a top-level guard.
AGENT_TIMEOUT_REQUEST_SECONDS = int(os.getenv("GBAW_AGENT_TIMEOUT_REQUEST_SECONDS", "180"))  # 3 min overall
AGENT_TIMEOUT_ORCHESTRATOR_SECONDS = int(os.getenv("GBAW_AGENT_TIMEOUT_ORCHESTRATOR_SECONDS", "150"))  # 2.5 min
AGENT_TIMEOUT_SPECIALIST_SECONDS = int(
    os.getenv("GBAW_AGENT_TIMEOUT_SPECIALIST_SECONDS", "90")
)  # 1.5 min per specialist

# Application-level rate limiting (Well-Architected GenAI Lens: Operational Excellence 2.2)
# Per-user request throttle to prevent system overload and runaway costs.
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("GBAW_RATE_LIMIT_MAX_REQUESTS", "10"))  # requests per window
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("GBAW_RATE_LIMIT_WINDOW_SECONDS", "60"))  # window in seconds

# Vector store embedding configuration (Well-Architected GenAI Lens: Cost 3.4, Performance Efficiency)
# Titan Embed v2 supports 256, 512, 1024 dimensions.
# Lower dimensions = cheaper storage/queries, faster retrieval, slightly lower quality.
# Default 1024 matches CloudFormation KB templates; override to test cost/quality tradeoffs.
EMBEDDING_DIMENSION = int(os.getenv("GBAW_EMBEDDING_DIMENSION", "1024"))

# =============================================================================
# BEDROCK QUOTA PLANNING (Well-Architected GenAI Lens: Reliability 1)
# =============================================================================
# Default Bedrock on-demand quotas (us-west-2, Claude Haiku 4.5 cross-region):
#   Requests per minute (RPM): 100   (cross-region inference profile)
#   Tokens per minute  (TPM): 200,000 input / 200,000 output
#
# Estimated usage per request:
#   Orchestrator: ~1,500 input tokens, ~500 output tokens
#   Specialist:   ~2,000 input tokens (incl. KB context), ~1,000 output tokens
#   Total:        ~3,500 input / ~1,500 output per user request
#
# At 10 concurrent users × 2 requests/min each = 20 RPM → well within 100 RPM.
# At 20 RPM × 3,500 input tokens = 70,000 TPM → within 200,000 TPM.
#
# Action items for production:
#   1. Monitor Bedrock throttling via CloudWatch: aws/bedrock ModelInvocationThrottles
#   2. Request quota increase if sustained RPM > 60 (60% of limit)
#   3. Consider Provisioned Throughput for predictable workloads (cost tradeoff)
#   4. Cross-region inference profiles provide automatic regional failover
#
# To request a quota increase:
#   aws service-quotas request-service-quota-increase \
#     --service-code bedrock --quota-code <quota-code> --desired-value <value>
BEDROCK_EXPECTED_RPM = int(os.getenv("GBAW_BEDROCK_EXPECTED_RPM", "20"))
BEDROCK_QUOTA_RPM = int(os.getenv("GBAW_BEDROCK_QUOTA_RPM", "100"))


# Per-agent inference parameters (Well-Architected GenAI Lens: Performance Efficiency 2)
# Haiku handles deterministic, low-latency orchestration. Sonnet handles
# deeper specialist reasoning over tool and Knowledge Base output. This role
# assignment is independent from retry and failure behavior.
class AgentInferenceConfig(TypedDict):
    """Inference settings passed directly to the Strands model constructor."""

    temperature: float
    max_tokens: int
    model_id: str


# The GameLift specialist generates complete CloudFormation templates (for
# example Agones-to-GameLift migrations). At 4096 tokens a full container-fleet
# template was cut off mid-resource, so it gets a larger output budget.
GAMELIFT_MAX_TOKENS = int(os.getenv("GBAW_GAMELIFT_MAX_TOKENS", "16000"))
# Hitting max_tokens raises (Strands MaxTokensReachedException) and fails the
# whole request, so the routing model gets headroom for long multi-specialist
# answers. Specialist IaC answers are relayed verbatim, not re-typed.
ORCHESTRATOR_MAX_TOKENS = int(os.getenv("GBAW_ORCHESTRATOR_MAX_TOKENS", "8192"))

INFERENCE_CONFIG: dict[str, AgentInferenceConfig] = {
    "orchestrator": {"temperature": 0.0, "max_tokens": ORCHESTRATOR_MAX_TOKENS, "model_id": ORCHESTRATOR_MODEL_ID},
    "gamelift": {"temperature": 0.1, "max_tokens": GAMELIFT_MAX_TOKENS, "model_id": SPECIALIST_MODEL_ID},
    "eks": {"temperature": 0.1, "max_tokens": 4096, "model_id": SPECIALIST_MODEL_ID},
    "cost": {"temperature": 0.0, "max_tokens": 4096, "model_id": SPECIALIST_MODEL_ID},
}

# Resilience settings (Well-Architected GenAI Lens: Reliability 2)
RETRY_MAX_ATTEMPTS = int(os.getenv("GBAW_RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("GBAW_RETRY_BASE_DELAY", "1.0"))

# Third-party packages
# boto3 client configuration (Well-Architected GenAI Lens: Reliability 5.2)
# Adaptive retry mode adds client-side rate limiting on top of standard retries,
# dynamically adjusting retry behavior based on error responses and throttling.
from botocore.config import Config as BotocoreConfig

BOTO3_RETRY_MODE = cast(Literal["legacy", "standard", "adaptive"], os.getenv("GBAW_BOTO3_RETRY_MODE", "adaptive"))
BOTO3_MAX_ATTEMPTS = int(os.getenv("GBAW_BOTO3_MAX_ATTEMPTS", "3"))
BOTO3_CLIENT_CONFIG = BotocoreConfig(
    # Supplying a config replaces Strands' 120-second Bedrock read timeout.
    read_timeout=120,
    retries={"mode": BOTO3_RETRY_MODE, "max_attempts": BOTO3_MAX_ATTEMPTS},
)


# MCP Configuration - All servers use stdio transport within AgentCore Runtime
# No HTTP endpoints or Parameter Store management needed

# Log configuration
logger.info(f"AWS Region: {AWS_REGION}")
logger.info(f"Orchestrator model: {ORCHESTRATOR_MODEL_ID}")
logger.info(f"Specialist model: {SPECIALIST_MODEL_ID}")

# Log MCP configuration
logger.info("MCP Configuration: All servers use stdio transport within AgentCore Runtime")
logger.info("- EKS MCP: stdio transport via console scripts (pre-installed packages)")
logger.info("- AWS API MCP: stdio transport via console scripts (pre-installed packages)")
logger.info("- Cost Explorer MCP: stdio transport via console scripts (pre-installed packages)")

# Log Memory configuration
logger.info(f"Bedrock Sessions Enabled: {USE_BEDROCK_SESSIONS}")
logger.info(f"Memory ID: {BEDROCK_AGENTCORE_MEMORY_ID or 'Not set (will be auto-configured)'}")
logger.info(f"Memory Config: Session TTL={MEMORY_SESSION_TTL_HOURS}h, User TTL={MEMORY_USER_TTL_DAYS}d")
logger.info(f"Long-term Memory Enabled: {MEMORY_LONG_TERM_ENABLED}")
logger.info(f"Memory Required (Hard Fail): {MEMORY_REQUIRED}")

# Log Knowledge Base configuration
logger.info("Knowledge Base Configuration:")
logger.info(f"  GameLift KB ID: {GAMELIFT_KB_ID or 'Not configured'}")
logger.info(f"  EKS KB ID: {EKS_KB_ID or 'Not configured'}")
logger.info(f"  Cost KB ID: {COST_KB_ID or 'Not configured'}")

# =============================================================================
# ADOT CONFIGURATION
# =============================================================================

# AWS Distro for OpenTelemetry (ADOT) - Auto-configured by AgentCore Runtime
# ADOT automatically detects AgentCore environment and configures:
# - Service name based on runtime
# - Resource attributes for AWS platform
# - CloudWatch export endpoints
# - Proper log group targeting

# Log ADOT configuration
logger.info("ADOT: Using AWS Distro for OpenTelemetry with auto-configuration")
logger.info("ADOT: AgentCore Runtime will auto-detect service name and export settings")

# =============================================================================
# DEPLOYMENT CONFIGURATION
# =============================================================================

# Project configuration
PROJECT_NAME = os.getenv("GBAW_PROJECT_NAME", "game-agent")

# Infrastructure sizing (CloudFormation parameters)
AGENTCORE_CPU = os.getenv("GBAW_AGENTCORE_CPU", "2048")
AGENTCORE_MEMORY = os.getenv("GBAW_AGENTCORE_MEMORY", "4096")
FRONTEND_CPU = os.getenv("GBAW_FRONTEND_CPU", "1024")
FRONTEND_MEMORY = os.getenv("GBAW_FRONTEND_MEMORY", "2048")
FRONTEND_PORT = int(os.getenv("GBAW_FRONTEND_PORT", "3000"))

# Environment detection
IS_DEVELOPMENT = ENV_FILE.exists()

# Development-only features
ENABLE_DEBUG_LOGGING = IS_DEVELOPMENT and os.getenv("GBAW_ENABLE_DEBUG_LOGGING", "true").lower() == "true"
SKIP_AUTH_IN_DEV = IS_DEVELOPMENT and os.getenv("NEXT_PUBLIC_SKIP_AUTH", "false").lower() == "true"

# Log deployment configuration
logger.info(f"Project: {PROJECT_NAME}")
logger.info(f"Environment: {'Development' if IS_DEVELOPMENT else 'Production'}")
logger.info(f"AgentCore Resources: {AGENTCORE_CPU} CPU / {AGENTCORE_MEMORY} MB")
logger.info(f"Frontend Resources: {FRONTEND_CPU} CPU / {FRONTEND_MEMORY} MB")
if IS_DEVELOPMENT:
    logger.info(f"Development Features: Debug={ENABLE_DEBUG_LOGGING}, Skip Auth={SKIP_AUTH_IN_DEV}")
