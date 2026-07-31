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


class TestSpecialistRoleFallback:
    """Unpinned specialists retain the specialist model role."""

    def test_unmapped_specialist_uses_specialist_role_model(self):
        # Local modules
        from agents.base_specialist import create_specialist_agent

        specialist_model = object()
        with (
            patch("agents.base_specialist.INFERENCE_CONFIG", {}),
            patch(
                "agents.base_specialist.create_specialist_bedrock_model",
                return_value=specialist_model,
            ) as create_specialist_model,
            patch("agents.base_specialist.create_bedrock_model_with_overrides") as create_override_model,
            patch("agents.base_specialist.Agent") as agent_class,
        ):
            agent_class.return_value.return_value = "specialist response"
            future_agent = create_specialist_agent(
                service_name="FutureService",
                emoji="",
                mcp_server_names=None,
                kb_id=None,
                prompt_fn=lambda: "system prompt",
            )

            assert future_agent("query") == "specialist response"

        create_specialist_model.assert_called_once_with()
        create_override_model.assert_not_called()
        assert agent_class.call_args.kwargs["model"] is specialist_model
