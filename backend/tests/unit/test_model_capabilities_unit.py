#!/usr/bin/env python3
"""Unit tests for per-model inference capabilities and the request config they produce.

These tests pin the exact Strands ``BedrockModel`` request shape for the two
default role models and for an earlier-generation override, so a rollback via
the ``GBAW_*`` model variables keeps the sampling behavior those models expect.

The default-shape tests patch ``models.cached_bedrock.ORCHESTRATOR_MODEL_ID``
and ``SPECIALIST_MODEL_ID`` to the ``DEFAULT_*`` constants so they assert what
the repository ships regardless of any role IDs resolved from a developer's
``ui/.env.local``, ``backend/.env.local``, or process environment.
"""

# Standard library
from unittest.mock import Mock, patch

# Third-party packages
import pytest

# Local modules
from config.model_capabilities import get_model_capabilities, reset_capability_warnings
from config.model_settings import DEFAULT_ORCHESTRATOR_MODEL_ID, DEFAULT_SPECIALIST_MODEL_ID
from config.settings import INFERENCE_CONFIG
from models.cached_bedrock import reset_model_cache

pytestmark = pytest.mark.unit


class TestModelCapabilityTable:
    """The capability table decides temperature and thinking per model generation."""

    def setup_method(self):
        reset_capability_warnings()

    def test_haiku_5_5_drops_temperature_and_disables_thinking(self):
        caps = get_model_capabilities("global.anthropic.claude-haiku-5-5")
        assert caps.send_temperature is False
        assert caps.thinking == {"type": "disabled"}

    def test_sonnet_5_5_drops_temperature_and_uses_between_tools(self):
        caps = get_model_capabilities("global.anthropic.claude-sonnet-5-5")
        assert caps.send_temperature is False
        assert caps.thinking == {"type": "between_tools"}

    def test_cross_region_prefixes_match(self):
        # The ``us.`` prefix resolves the same as ``global.``.
        assert get_model_capabilities("us.anthropic.claude-sonnet-5-5").thinking == {"type": "between_tools"}

    def test_earlier_claude_keeps_temperature_and_no_thinking(self):
        caps = get_model_capabilities("global.anthropic.claude-haiku-4-5-20251001-v1:0")
        assert caps.send_temperature is True
        assert caps.thinking is None

    def test_unmatched_profile_falls_back_and_warns(self):
        with patch("config.model_capabilities.logger") as mock_logger:
            caps = get_model_capabilities(
                "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abcd1234"
            )
        assert caps.send_temperature is True
        assert caps.thinking is None
        mock_logger.warning.assert_called_once()
        # The warning tells the operator the profile is rejected because it
        # receives temperature, and points to the system profile IDs.
        message = mock_logger.warning.call_args.args[0]
        assert "temperature" in message
        assert "rejected" in message
        assert "global." in message

    def test_unmatched_id_warns_once_per_distinct_id(self):
        # The lookup runs on every model construction, so the fallback warning
        # must fire once per distinct ID, not once per request.
        profile_id = "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abcd1234"
        with patch("config.model_capabilities.logger") as mock_logger:
            get_model_capabilities(profile_id)
            get_model_capabilities(profile_id)
        assert mock_logger.warning.call_count == 1


class TestDefaultModelRequestConfig:
    """The default role models pin a request config that the new models accept.

    The role IDs are patched to the shipped ``DEFAULT_*`` constants so these
    assertions do not depend on the developer's resolved environment.
    """

    def setup_method(self):
        reset_model_cache()

    @patch("models.cached_bedrock.ORCHESTRATOR_MODEL_ID", DEFAULT_ORCHESTRATOR_MODEL_ID)
    @patch("models.cached_bedrock.BedrockModel")
    def test_orchestrator_default_omits_temperature_and_disables_thinking(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        mock_bedrock_model.return_value = Mock()
        create_cached_bedrock_model()

        kwargs = mock_bedrock_model.call_args.kwargs
        assert kwargs["model_id"] == DEFAULT_ORCHESTRATOR_MODEL_ID
        assert "temperature" not in kwargs
        assert kwargs["additional_request_fields"] == {"thinking": {"type": "disabled"}}
        # Caching, tool use, and max tokens are preserved.
        assert kwargs["cache_prompt"] == "default"
        assert kwargs["cache_tools"] == "default"
        assert kwargs["max_tokens"] == 4096

    @patch("models.cached_bedrock.SPECIALIST_MODEL_ID", DEFAULT_SPECIALIST_MODEL_ID)
    @patch("models.cached_bedrock.BedrockModel")
    def test_specialist_default_omits_temperature_and_uses_between_tools(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import create_specialist_bedrock_model

        mock_bedrock_model.return_value = Mock()
        create_specialist_bedrock_model()

        kwargs = mock_bedrock_model.call_args.kwargs
        assert kwargs["model_id"] == DEFAULT_SPECIALIST_MODEL_ID
        assert "temperature" not in kwargs
        assert kwargs["additional_request_fields"] == {"thinking": {"type": "between_tools"}}
        assert kwargs["cache_prompt"] == "default"
        assert kwargs["cache_tools"] == "default"


class TestPerRequestConstructorRequestConfig:
    """Every chat turn builds a model through ``create_bedrock_model_with_overrides``.

    Parametrizing over the ``INFERENCE_CONFIG`` keys guards the per-request path
    that serves orchestrator and specialist turns: with the 5.5 defaults, none
    of them send ``temperature`` and each sends the ``thinking`` value its model
    accepts.
    """

    def setup_method(self):
        reset_model_cache()

    @pytest.mark.parametrize("agent_key", list(INFERENCE_CONFIG.keys()))
    @patch("models.cached_bedrock.BedrockModel")
    def test_default_per_request_shape_omits_temperature_and_shapes_thinking(self, mock_bedrock_model, agent_key):
        # Local modules
        from models.cached_bedrock import create_bedrock_model_with_overrides

        inf = INFERENCE_CONFIG[agent_key]
        expected = get_model_capabilities(inf["model_id"])

        mock_bedrock_model.return_value = Mock()
        create_bedrock_model_with_overrides(
            temperature=inf["temperature"], max_tokens=inf["max_tokens"], model_id=inf["model_id"]
        )

        kwargs = mock_bedrock_model.call_args.kwargs
        assert kwargs["model_id"] == inf["model_id"]
        # The request shape matches the model's capabilities: a model that
        # rejects temperature gets none and a thinking field; an earlier model
        # keeps temperature and gets no thinking field. With the shipped 5.5
        # defaults every key takes the former path.
        if expected.send_temperature:
            assert kwargs["temperature"] == inf["temperature"]
            assert "additional_request_fields" not in kwargs
        else:
            assert "temperature" not in kwargs
            assert kwargs["additional_request_fields"] == {"thinking": expected.thinking}


class TestOverrideModelRequestConfig:
    """An earlier-generation override keeps temperature and sends no thinking field."""

    def setup_method(self):
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel")
    def test_earlier_model_override_keeps_temperature_no_thinking(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import create_bedrock_model_with_overrides

        mock_bedrock_model.return_value = Mock()
        create_bedrock_model_with_overrides(
            temperature=0.0, max_tokens=4096, model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

        kwargs = mock_bedrock_model.call_args.kwargs
        assert kwargs["model_id"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert kwargs["temperature"] == 0.0
        assert "additional_request_fields" not in kwargs
