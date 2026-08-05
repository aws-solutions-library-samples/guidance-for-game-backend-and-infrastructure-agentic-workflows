"""Canonical role-based Bedrock model configuration.

The orchestrator and specialists are independent roles, not a failover order.
Canonical role variables take precedence over legacy compatibility aliases.
"""

# Standard library
import os
from collections.abc import Mapping

# Third-party packages
from loguru import logger

DEFAULT_ORCHESTRATOR_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_SPECIALIST_MODEL_ID = "global.anthropic.claude-sonnet-4-6"

ORCHESTRATOR_MODEL_ENV = "GBAW_ORCHESTRATOR_MODEL_ID"
SPECIALIST_MODEL_ENV = "GBAW_SPECIALIST_MODEL_ID"
LEGACY_ORCHESTRATOR_MODEL_ENV = "GBAW_BEDROCK_MODEL_ID"
LEGACY_SPECIALIST_MODEL_ENV = "GBAW_BEDROCK_MODEL_ID_SECONDARY"


def _resolve_role_model(
    env: Mapping[str, str], canonical_env: str, legacy_env: str, default_model_id: str, role: str
) -> str:
    """Resolve one role, warning when a legacy alias supplies the value.

    A legacy alias left over from an earlier release would otherwise keep an
    old model silently, so the role never picks up the current default. The
    warning makes that override visible in startup and deployment logs.
    """
    canonical_value = env.get(canonical_env)
    if canonical_value:
        return canonical_value

    legacy_value = env.get(legacy_env)
    if legacy_value:
        logger.warning(
            f"{legacy_env} is deprecated and is overriding the {role} default "
            f"({default_model_id}); set {canonical_env} instead. Using: {legacy_value}"
        )
        return legacy_value

    return default_model_id


def resolve_model_ids(environment: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Resolve role model IDs with canonical, legacy, then default precedence.

    Empty values are treated as unset. The legacy names remain supported for
    existing deployments, but new configuration should use the role names.
    """
    env = os.environ if environment is None else environment
    orchestrator_model_id = _resolve_role_model(
        env, ORCHESTRATOR_MODEL_ENV, LEGACY_ORCHESTRATOR_MODEL_ENV, DEFAULT_ORCHESTRATOR_MODEL_ID, "orchestrator"
    )
    specialist_model_id = _resolve_role_model(
        env, SPECIALIST_MODEL_ENV, LEGACY_SPECIALIST_MODEL_ENV, DEFAULT_SPECIALIST_MODEL_ID, "specialist"
    )
    return orchestrator_model_id, specialist_model_id
