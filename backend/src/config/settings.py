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


INFERENCE_CONFIG: dict[str, AgentInferenceConfig] = {
    "orchestrator": {"temperature": 0.0, "max_tokens": 4096, "model_id": ORCHESTRATOR_MODEL_ID},
    "gamelift": {"temperature": 0.1, "max_tokens": 4096, "model_id": SPECIALIST_MODEL_ID},
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
