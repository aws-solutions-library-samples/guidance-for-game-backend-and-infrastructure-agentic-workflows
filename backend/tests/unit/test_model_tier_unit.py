"""Unit tests for explicit orchestrator and specialist model selection."""

# Standard library
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


class TestInferenceConfigModelRoles:
    """INFERENCE_CONFIG pins the intended model for every agent role."""

    def test_all_agents_use_their_role_model(self):
        # Local modules
        from config.settings import INFERENCE_CONFIG, ORCHESTRATOR_MODEL_ID, SPECIALIST_MODEL_ID

        assert INFERENCE_CONFIG["orchestrator"]["model_id"] == ORCHESTRATOR_MODEL_ID
        for specialist in ("gamelift", "eks", "cost"):
            assert INFERENCE_CONFIG[specialist]["model_id"] == SPECIALIST_MODEL_ID

    def test_default_role_models_are_distinct(self):
        # Local modules
        from config.model_settings import DEFAULT_ORCHESTRATOR_MODEL_ID, DEFAULT_SPECIALIST_MODEL_ID

        assert DEFAULT_ORCHESTRATOR_MODEL_ID != DEFAULT_SPECIALIST_MODEL_ID
        assert DEFAULT_SPECIALIST_MODEL_ID == "global.anthropic.claude-sonnet-4-6"


class TestCreateModelHonorsModelId:
    """The model constructor passes the selected role model through unchanged."""

    def test_explicit_model_id_is_used(self):
        # Local modules
        from models.cached_bedrock import create_bedrock_model_with_overrides

        with patch("models.cached_bedrock.BedrockModel") as mock_model:
            create_bedrock_model_with_overrides(temperature=0.1, max_tokens=4096, model_id="custom-model-xyz")
            assert mock_model.call_args.kwargs["model_id"] == "custom-model-xyz"

    def test_empty_model_id_uses_orchestrator_for_compatibility(self):
        # Local modules
        from config.settings import ORCHESTRATOR_MODEL_ID
        from models.cached_bedrock import create_bedrock_model_with_overrides

        with patch("models.cached_bedrock.BedrockModel") as mock_model:
            create_bedrock_model_with_overrides()
            assert mock_model.call_args.kwargs["model_id"] == ORCHESTRATOR_MODEL_ID
