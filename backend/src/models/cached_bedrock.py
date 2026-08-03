"""Cached Bedrock model configuration.

All configured models use prompt caching and the same optional Guardrail
settings. Orchestrator and specialist roles are selected explicitly; they are
not used as implicit fallbacks for one another.
"""

# Standard library
import os
import threading

# Third-party packages
from strands.models import BedrockModel

# Local modules
from config.settings import (
    AWS_REGION,
    BEDROCK_GUARDRAIL_ENABLED,
    BEDROCK_GUARDRAIL_ID,
    BEDROCK_GUARDRAIL_VERSION,
    BOTO3_CLIENT_CONFIG,
    ORCHESTRATOR_MODEL_ID,
    SPECIALIST_MODEL_ID,
)
from utils.logger import logger

BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))

_orchestrator_model = None
_specialist_model = None
_model_lock = threading.Lock()


def _model_config(model_id: str, temperature: float, max_tokens: int) -> dict:
    """Build shared Strands configuration without changing the selected role."""
    config = {
        "model_id": model_id,
        "boto_client_config": BOTO3_CLIENT_CONFIG,
        "region_name": AWS_REGION,
        "cache_prompt": "default",
        "cache_tools": "default",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if BEDROCK_GUARDRAIL_ENABLED and BEDROCK_GUARDRAIL_ID:
        config["guardrail_id"] = BEDROCK_GUARDRAIL_ID
        config["guardrail_version"] = BEDROCK_GUARDRAIL_VERSION
        config["guardrail_trace"] = "enabled"
        config["guardrail_latest_message"] = True
        logger.debug(f"Guardrails enabled: {BEDROCK_GUARDRAIL_ID}")

    return config


def create_cached_bedrock_model():
    """Get the singleton orchestrator model used for startup prewarming.

    Model construction failures propagate to the caller. Runtime retries are
    controlled by the shared Botocore adaptive retry configuration; there is no
    implicit cross-role model fallback.
    """
    global _orchestrator_model

    if _orchestrator_model is not None:
        return _orchestrator_model

    with _model_lock:
        if _orchestrator_model is None:
            _orchestrator_model = BedrockModel(
                **_model_config(ORCHESTRATOR_MODEL_ID, temperature=0.1, max_tokens=BEDROCK_MAX_TOKENS)
            )
            logger.info(f"Orchestrator model ready: {ORCHESTRATOR_MODEL_ID} (singleton)")

    return _orchestrator_model


def create_specialist_bedrock_model():
    """Get a singleton specialist model with shared cache and Guardrail settings."""
    global _specialist_model

    if _specialist_model is not None:
        return _specialist_model

    with _model_lock:
        if _specialist_model is None:
            _specialist_model = BedrockModel(
                **_model_config(SPECIALIST_MODEL_ID, temperature=0.1, max_tokens=BEDROCK_MAX_TOKENS)
            )
            logger.info(f"Specialist model ready: {SPECIALIST_MODEL_ID} (singleton)")

    return _specialist_model


def reset_model_cache():
    """Reset model singletons. Useful for tests or process-level config changes."""
    global _orchestrator_model, _specialist_model
    with _model_lock:
        _orchestrator_model = None
        _specialist_model = None
        logger.debug("Model cache reset")


def create_bedrock_model_with_overrides(temperature: float = 0.1, max_tokens: int = 4096, model_id: str = ""):
    """Create a model with per-agent inference parameters.

    Args:
        temperature: Sampling temperature (0.0 is deterministic).
        max_tokens: Maximum output tokens.
        model_id: Explicit role model ID. Empty values use the orchestrator ID
            for compatibility with existing callers.
    """
    selected_model_id = model_id or ORCHESTRATOR_MODEL_ID
    model_config = _model_config(selected_model_id, temperature, max_tokens)
    logger.info(f"Configured model: {selected_model_id} (temperature={temperature})")
    return BedrockModel(**model_config)
