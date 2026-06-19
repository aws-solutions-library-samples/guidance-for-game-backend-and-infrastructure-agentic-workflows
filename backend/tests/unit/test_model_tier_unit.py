"""Unit tests for per-agent model-tier selection (issue #122).

The orchestrator must run on the fast PRIMARY model (Haiku) and the domain
specialists on the more capable SECONDARY model (Sonnet). Regression guard:
previously every agent silently inherited the primary because INFERENCE_CONFIG
carried no model_id.
"""

# Standard library
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


class TestInferenceConfigModelTier:
    """INFERENCE_CONFIG pins the intended model per agent."""

    def test_specialists_use_secondary_orchestrator_uses_primary(self):
        # Local modules
        from config.settings import BEDROCK_MODEL_ID, BEDROCK_MODEL_ID_SECONDARY, INFERENCE_CONFIG

        assert INFERENCE_CONFIG["orchestrator"]["model_id"] == BEDROCK_MODEL_ID
        for specialist in ("gamelift", "eks", "cost"):
            assert (
                INFERENCE_CONFIG[specialist]["model_id"] == BEDROCK_MODEL_ID_SECONDARY
            ), f"{specialist} should run on the secondary (Sonnet) model"

    def test_primary_and_secondary_are_distinct(self):
        # Local modules
        from config.settings import BEDROCK_MODEL_ID, BEDROCK_MODEL_ID_SECONDARY

        assert BEDROCK_MODEL_ID != BEDROCK_MODEL_ID_SECONDARY


class TestCreateModelHonorsModelId:
    """create_bedrock_model_with_overrides passes the requested model_id through."""

    def test_explicit_model_id_is_used(self):
        # Local modules
        from models.cached_bedrock import create_bedrock_model_with_overrides

        with patch("models.cached_bedrock.BedrockModel") as mock_model:
            create_bedrock_model_with_overrides(temperature=0.1, max_tokens=4096, model_id="custom-model-xyz")
            assert mock_model.call_args.kwargs["model_id"] == "custom-model-xyz"

    def test_defaults_to_primary_when_model_id_omitted(self):
        # Local modules
        from config.settings import BEDROCK_MODEL_ID
        from models.cached_bedrock import create_bedrock_model_with_overrides

        with patch("models.cached_bedrock.BedrockModel") as mock_model:
            create_bedrock_model_with_overrides()
            assert mock_model.call_args.kwargs["model_id"] == BEDROCK_MODEL_ID
