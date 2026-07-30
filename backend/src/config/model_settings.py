"""Canonical role-based Bedrock model configuration.

The orchestrator and specialists are independent roles, not a failover order.
Canonical role variables take precedence over legacy compatibility aliases.
"""

# Standard library
import os
from collections.abc import Mapping

DEFAULT_ORCHESTRATOR_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_SPECIALIST_MODEL_ID = "global.anthropic.claude-sonnet-4-6"

ORCHESTRATOR_MODEL_ENV = "GBAW_ORCHESTRATOR_MODEL_ID"
SPECIALIST_MODEL_ENV = "GBAW_SPECIALIST_MODEL_ID"
LEGACY_ORCHESTRATOR_MODEL_ENV = "GBAW_BEDROCK_MODEL_ID"
LEGACY_SPECIALIST_MODEL_ENV = "GBAW_BEDROCK_MODEL_ID_SECONDARY"


def resolve_model_ids(environment: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Resolve role model IDs with canonical, legacy, then default precedence.

    Empty values are treated as unset. The legacy names remain supported for
    existing deployments, but new configuration should use the role names.
    """
    env = os.environ if environment is None else environment
    orchestrator_model_id = (
        env.get(ORCHESTRATOR_MODEL_ENV)
        or env.get(LEGACY_ORCHESTRATOR_MODEL_ENV)
        or DEFAULT_ORCHESTRATOR_MODEL_ID
    )
    specialist_model_id = (
        env.get(SPECIALIST_MODEL_ENV)
        or env.get(LEGACY_SPECIALIST_MODEL_ENV)
        or DEFAULT_SPECIALIST_MODEL_ID
    )
    return orchestrator_model_id, specialist_model_id
