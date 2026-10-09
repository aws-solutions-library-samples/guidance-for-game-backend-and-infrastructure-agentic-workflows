#!/usr/bin/env python3
"""Unit tests for per-model inference capabilities and the request config they produce.

These tests pin the exact Strands ``BedrockModel`` request shape for the two
default role models and for an earlier-generation override, so a rollback via
the ``GBAW_*`` model variables keeps the sampling behavior those models expect.
"""

# Standard library
from unittest.mock import Mock, patch

# Third-party packages
import pytest

# Local modules
from config.model_capabilities import get_model_capabilities
from models.cached_bedrock import reset_model_cache

pytestmark = pytest.mark.unit


class TestModelCapabilityTable:
    """The capability table decides temperature and thinking per model generation."""

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


class TestDefaultModelRequestConfig:
    """The default role models pin a request config that the new models accept."""

    def setup_method(self):
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel")
    def test_orchestrator_default_omits_temperature_and_disables_thinking(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        mock_bedrock_model.return_value = Mock()
        create_cached_bedrock_model()

        kwargs = mock_bedrock_model.call_args.kwargs
        assert kwargs["model_id"] == "global.anthropic.claude-haiku-5-5"
        assert "temperature" not in kwargs
        assert kwargs["additional_request_fields"] == {"thinking": {"type": "disabled"}}
        # Caching, tool use, and max tokens are preserved.
        assert kwargs["cache_prompt"] == "default"
        assert kwargs["cache_tools"] == "default"
        assert kwargs["max_tokens"] == 4096

    @patch("models.cached_bedrock.BedrockModel")
    def test_specialist_default_omits_temperature_and_uses_between_tools(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import create_specialist_bedrock_model

        mock_bedrock_model.return_value = Mock()
        create_specialist_bedrock_model()

        kwargs = mock_bedrock_model.call_args.kwargs
        assert kwargs["model_id"] == "global.anthropic.claude-sonnet-5-5"
        assert "temperature" not in kwargs
        assert kwargs["additional_request_fields"] == {"thinking": {"type": "between_tools"}}
        assert kwargs["cache_prompt"] == "default"
        assert kwargs["cache_tools"] == "default"


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
