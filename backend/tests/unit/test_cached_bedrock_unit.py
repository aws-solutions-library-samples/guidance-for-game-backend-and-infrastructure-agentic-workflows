#!/usr/bin/env python3
"""Unit tests for cached Bedrock model - behavioral testing."""

# Standard library
import os
import sys
from unittest.mock import Mock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from models.cached_bedrock import reset_model_cache


class TestCachedBedrockModel:
    """Test cached Bedrock model creation behavior."""

    def setup_method(self):
        """Reset model cache before each test."""
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel")
    def test_creates_cached_model_always(self, mock_bedrock_model):
        """Test always creates cached model (caching enabled by default)."""
        mock_model = Mock()
        mock_bedrock_model.return_value = mock_model

        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        result = create_cached_bedrock_model()

        # Verify BedrockModel was called with caching parameters
        call_args = mock_bedrock_model.call_args[1]
        assert call_args["cache_prompt"] == "default"
        assert call_args["cache_tools"] == "default"
        assert call_args["temperature"] == 0.1
        assert call_args["max_tokens"] == 4096
        assert result == mock_model

    @patch("models.cached_bedrock.BedrockModel")
    def test_does_not_mutate_global_aws_profile_env(self, mock_bedrock_model):
        """Model creation must NOT write os.environ['AWS_PROFILE'] (#131).

        The old code re-asserted AWS_PROFILE into the process env on every model
        build — a global-state side effect (boto3 reads the profile from the
        session/env already). The module no longer references AWS_PROFILE at all;
        confirm building a model leaves the env exactly as it found it.
        """
        mock_bedrock_model.return_value = Mock()
        original_profile = os.environ.get("AWS_PROFILE")

        try:
            os.environ.pop("AWS_PROFILE", None)
            # Local modules
            from models.cached_bedrock import create_cached_bedrock_model

            reset_model_cache()
            create_cached_bedrock_model()

            # The module must not have introduced AWS_PROFILE into the process env.
            assert "AWS_PROFILE" not in os.environ
        finally:
            if original_profile is not None:
                os.environ["AWS_PROFILE"] = original_profile
            elif "AWS_PROFILE" in os.environ:
                del os.environ["AWS_PROFILE"]


class TestSecondaryBedrockModel:
    """Test secondary Bedrock model creation behavior."""

    def setup_method(self):
        """Reset model cache before each test."""
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel")
    def test_creates_secondary_cached_model(self, mock_bedrock_model):
        """Test creates secondary model with caching."""
        mock_model = Mock()
        mock_bedrock_model.return_value = mock_model

        # Local modules
        from models.cached_bedrock import create_secondary_bedrock_model

        result = create_secondary_bedrock_model()

        # Verify BedrockModel was called with caching parameters
        call_args = mock_bedrock_model.call_args[1]
        assert call_args["cache_prompt"] == "default"
        assert call_args["cache_tools"] == "default"
        assert call_args["temperature"] == 0.1
        assert call_args["max_tokens"] == 4096
        assert result == mock_model


class TestModelFallback:
    """Test automatic fallback from primary to secondary model."""

    def setup_method(self):
        """Reset model cache before each test."""
        reset_model_cache()

    @patch("models.cached_bedrock.BedrockModel")
    def test_fallback_to_secondary_when_primary_fails(self, mock_bedrock_model):
        """Test automatic fallback to secondary model when primary fails."""
        mock_model = Mock()
        mock_bedrock_model.side_effect = [Exception("Primary model unavailable"), mock_model]

        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        result = create_cached_bedrock_model()

        assert mock_bedrock_model.call_count == 2
        assert result == mock_model

    @patch("models.cached_bedrock.BedrockModel")
    def test_fallback_uses_caching(self, mock_bedrock_model):
        """Test fallback model also uses caching."""
        mock_model = Mock()
        mock_bedrock_model.side_effect = [Exception("Primary unavailable"), mock_model]

        # Local modules
        from models.cached_bedrock import create_cached_bedrock_model

        create_cached_bedrock_model()

        # Check second call (fallback) used caching
        second_call_args = mock_bedrock_model.call_args_list[1][1]
        assert second_call_args["cache_prompt"] == "default"
        assert second_call_args["cache_tools"] == "default"


class TestMaxTokensEnvOverride:
    """Test BEDROCK_MAX_TOKENS environment variable override."""

    def setup_method(self):
        """Reset model cache before each test."""
        reset_model_cache()

    def test_default_max_tokens_is_4096(self):
        """Test default max_tokens is 4096 when env var not set."""
        # Local modules
        from models.cached_bedrock import BEDROCK_MAX_TOKENS

        # Default should be 4096
        assert BEDROCK_MAX_TOKENS == 4096

    @patch("models.cached_bedrock.BedrockModel")
    def test_max_tokens_used_in_model_config(self, mock_bedrock_model):
        """Test BEDROCK_MAX_TOKENS is used in model configuration."""
        mock_model = Mock()
        mock_bedrock_model.return_value = mock_model

        # Local modules
        from models.cached_bedrock import BEDROCK_MAX_TOKENS, create_cached_bedrock_model

        reset_model_cache()
        create_cached_bedrock_model()

        call_args = mock_bedrock_model.call_args[1]
        assert call_args["max_tokens"] == BEDROCK_MAX_TOKENS
