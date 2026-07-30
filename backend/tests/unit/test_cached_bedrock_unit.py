#!/usr/bin/env python3
"""Unit tests for cached Bedrock model behavior."""

# Standard library
import os
from unittest.mock import Mock, patch

# Third-party packages
import pytest

# Local modules
from models.cached_bedrock import reset_model_cache

pytestmark = pytest.mark.unit


class TestCachedBedrockModels:
    """Cached role models preserve caching and role selection."""

    def setup_method(self):
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel")
    def test_creates_cached_orchestrator_model(self, mock_bedrock_model):
        # Local modules
        from config.settings import AWS_REGION, ORCHESTRATOR_MODEL_ID
        from models.cached_bedrock import BOTO3_CLIENT_CONFIG as MODEL_CLIENT_CONFIG
        from models.cached_bedrock import create_cached_bedrock_model

        mock_model = Mock()
        mock_bedrock_model.return_value = mock_model
        result = create_cached_bedrock_model()

        call_args = mock_bedrock_model.call_args.kwargs
        assert call_args["model_id"] == ORCHESTRATOR_MODEL_ID
        assert call_args["boto_client_config"] is MODEL_CLIENT_CONFIG
        assert call_args["region_name"] == AWS_REGION
        assert call_args["cache_prompt"] == "default"
        assert call_args["cache_tools"] == "default"
        assert call_args["temperature"] == 0.1
        assert call_args["max_tokens"] == 4096
        assert result == mock_model

    @patch("models.cached_bedrock.BedrockModel")
    def test_creates_cached_specialist_model(self, mock_bedrock_model):
        # Local modules
        from config.settings import AWS_REGION, SPECIALIST_MODEL_ID
        from models.cached_bedrock import BOTO3_CLIENT_CONFIG as MODEL_CLIENT_CONFIG
        from models.cached_bedrock import create_specialist_bedrock_model

        mock_model = Mock()
        mock_bedrock_model.return_value = mock_model
        result = create_specialist_bedrock_model()

        call_args = mock_bedrock_model.call_args.kwargs
        assert call_args["model_id"] == SPECIALIST_MODEL_ID
        assert call_args["boto_client_config"] is MODEL_CLIENT_CONFIG
        assert call_args["region_name"] == AWS_REGION
        assert call_args["cache_prompt"] == "default"
        assert call_args["cache_tools"] == "default"
        assert result == mock_model

    @patch("models.cached_bedrock.BedrockModel")
    def test_does_not_mutate_global_aws_profile_env(self, mock_bedrock_model):
        mock_bedrock_model.return_value = Mock()
        original_profile = os.environ.pop("AWS_PROFILE", None)
        try:
            # Local modules
            from models.cached_bedrock import create_cached_bedrock_model

            create_cached_bedrock_model()
            assert "AWS_PROFILE" not in os.environ
        finally:
            if original_profile is not None:
                os.environ["AWS_PROFILE"] = original_profile


class TestModelFailureBehavior:
    """Role selection is independent from failure handling."""

    def setup_method(self):
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel", side_effect=RuntimeError("model unavailable"))
    def test_construction_failure_propagates_without_cross_role_fallback(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        with pytest.raises(RuntimeError, match="model unavailable"):
            create_cached_bedrock_model()

        mock_bedrock_model.assert_called_once()


class TestMaxTokensEnvOverride:
    """Test the cached model max-token setting."""

    def setup_method(self):
        reset_model_cache()

    def test_default_max_tokens_is_4096(self):
        # Local modules
        from models.cached_bedrock import BEDROCK_MAX_TOKENS

        assert BEDROCK_MAX_TOKENS == 4096

    @patch("models.cached_bedrock.BedrockModel")
    def test_max_tokens_used_in_model_config(self, mock_bedrock_model):
        # Local modules
        from models.cached_bedrock import BEDROCK_MAX_TOKENS, create_cached_bedrock_model

        mock_bedrock_model.return_value = Mock()
        create_cached_bedrock_model()
        assert mock_bedrock_model.call_args.kwargs["max_tokens"] == BEDROCK_MAX_TOKENS
