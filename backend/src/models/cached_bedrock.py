"""
Cached Bedrock model configuration.

All models use prompt caching by default (Claude 4+ models support it).
Application inference profiles inherit caching from underlying foundation models.

Performance Optimization:
- Uses singleton pattern to reuse model instance across requests
- Thread-safe initialization with double-checked locking
- Saves 200-300ms per query by avoiding repeated model creation
"""

# Standard library
import os
import threading

# Third-party packages
from strands.models import BedrockModel

# Local modules
from config.settings import (
    BEDROCK_GUARDRAIL_ENABLED,
    BEDROCK_GUARDRAIL_ID,
    BEDROCK_GUARDRAIL_VERSION,
    BEDROCK_MODEL_ID,
    BEDROCK_MODEL_ID_SECONDARY,
)
from utils.logger import logger

# Configurable max tokens (env override for handling verbose responses)
BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))

# Singleton model instances (thread-safe)
_primary_model = None
_secondary_model = None
_model_lock = threading.Lock()


def create_cached_bedrock_model():
    """
    Get or create singleton Bedrock model with caching and optional guardrails.

    Uses double-checked locking pattern for thread-safe lazy initialization.
    The model instance is reused across all requests to avoid creation overhead.

    Returns:
        BedrockModel: Singleton model instance (primary or fallback)
    """
    global _primary_model

    # Fast path: return cached model if available (no lock needed)
    if _primary_model is not None:
        return _primary_model

    # Slow path: acquire lock and initialize
    with _model_lock:
        # Double-check after acquiring lock (another thread may have initialized)
        if _primary_model is not None:
            return _primary_model

        # Base model configuration
        model_config = {
            "model_id": BEDROCK_MODEL_ID,
            "cache_prompt": "default",
            "cache_tools": "default",
            "temperature": 0.1,
            "max_tokens": BEDROCK_MAX_TOKENS,
        }

        # Add guardrails if enabled and configured. strands BedrockModel takes
        # FLAT guardrail_id / guardrail_version (a nested guardrail_config dict
        # is silently dropped — strands warns but does not raise — which leaves
        # guardrails disabled at runtime).
        # guardrail_latest_message: input policies must evaluate ONLY the new
        # user message — without it the PROMPT_ATTACK filter scans the entire
        # AgentCore-Memory replayed history and false-blocks valid follow-ups
        # (issue #201).
        if BEDROCK_GUARDRAIL_ENABLED and BEDROCK_GUARDRAIL_ID:
            model_config["guardrail_id"] = BEDROCK_GUARDRAIL_ID
            model_config["guardrail_version"] = BEDROCK_GUARDRAIL_VERSION
            model_config["guardrail_trace"] = "enabled"
            model_config["guardrail_latest_message"] = True
            logger.debug(f"🛡️ Guardrails enabled: {BEDROCK_GUARDRAIL_ID}")

        try:
            _primary_model = BedrockModel(**model_config)
            logger.info(f"✅ Engines online: {BEDROCK_MODEL_ID} (singleton)")
            return _primary_model

        except Exception as e:
            logger.warning(f"⚠️ Primary model {BEDROCK_MODEL_ID} failed: {e}")
            logger.info(f"🔄 Falling back to secondary model: {BEDROCK_MODEL_ID_SECONDARY}")

            # Fallback model configuration
            fallback_config = {
                "model_id": BEDROCK_MODEL_ID_SECONDARY,
                "cache_prompt": "default",
                "cache_tools": "default",
                "temperature": 0.1,
                "max_tokens": BEDROCK_MAX_TOKENS,
            }

            # Apply guardrails to fallback too (flat params + latest-message, see above)
            if BEDROCK_GUARDRAIL_ENABLED and BEDROCK_GUARDRAIL_ID:
                fallback_config["guardrail_id"] = BEDROCK_GUARDRAIL_ID
                fallback_config["guardrail_version"] = BEDROCK_GUARDRAIL_VERSION
                fallback_config["guardrail_trace"] = "enabled"
                fallback_config["guardrail_latest_message"] = True

            _primary_model = BedrockModel(**fallback_config)
            logger.info(f"✅ Fallback engines online: {BEDROCK_MODEL_ID_SECONDARY} (singleton)")
            return _primary_model


def create_secondary_bedrock_model():
    """Get or create singleton secondary model (cost-effective option)."""
    global _secondary_model

    if _secondary_model is not None:
        return _secondary_model

    with _model_lock:
        if _secondary_model is not None:
            return _secondary_model

        _secondary_model = BedrockModel(
            model_id=BEDROCK_MODEL_ID_SECONDARY,
            cache_prompt="default",
            cache_tools="default",
            temperature=0.1,
            max_tokens=BEDROCK_MAX_TOKENS,
        )
        logger.info(f"✅ Secondary engines online: {BEDROCK_MODEL_ID_SECONDARY} (singleton)")
        return _secondary_model


def reset_model_cache():
    """Reset model singletons. Useful for testing or config changes."""
    global _primary_model, _secondary_model
    with _model_lock:
        _primary_model = None
        _secondary_model = None
        logger.debug("🔄 Model cache reset")


def create_bedrock_model_with_overrides(temperature: float = 0.1, max_tokens: int = 4096, model_id: str = ""):
    """Create a non-singleton BedrockModel with custom inference parameters.

    Used by specialist agents that need per-agent tuning (e.g. orchestrator
    uses temp=0.0 for deterministic routing; specialists run on Sonnet for
    deeper reasoning while the orchestrator stays on Haiku for fast routing).

    Args:
        temperature: Sampling temperature (0.0 = deterministic, 1.0 = creative).
        max_tokens: Maximum output tokens.
        model_id: Bedrock model id to use. Defaults to the primary
            (BEDROCK_MODEL_ID) when empty, preserving prior behavior.

    Returns:
        BedrockModel configured with the given parameters.
    """
    model_config = {
        "model_id": model_id or BEDROCK_MODEL_ID,
        "cache_prompt": "default",
        "cache_tools": "default",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if BEDROCK_GUARDRAIL_ENABLED and BEDROCK_GUARDRAIL_ID:
        model_config["guardrail_id"] = BEDROCK_GUARDRAIL_ID
        model_config["guardrail_version"] = BEDROCK_GUARDRAIL_VERSION
        model_config["guardrail_trace"] = "enabled"
        model_config["guardrail_latest_message"] = True

    # Log the selected model so per-agent tier is observable at runtime — the
    # singleton path logs "Engines online", but these per-agent models were
    # otherwise silent, hiding which tier each specialist actually used.
    logger.info(f"🧠 Model (overrides): {model_config['model_id']} (temp={temperature})")

    return BedrockModel(**model_config)
