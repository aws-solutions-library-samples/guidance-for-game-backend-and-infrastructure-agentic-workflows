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
    def test_sets_aws_profile_when_provided(self, mock_bedrock_model):
        """Test sets AWS_PROFILE environment variable when provided."""
        mock_model = Mock()
        mock_bedrock_model.return_value = mock_model

        # Save original AWS_PROFILE
        original_profile = os.environ.get("AWS_PROFILE")

        try:
            with patch("models.cached_bedrock.AWS_PROFILE", "test-profile"):
                # Local modules
                from models.cached_bedrock import create_cached_bedrock_model

                reset_model_cache()  # Reset after patch to ensure fresh creation
                create_cached_bedrock_model()

            assert os.environ.get("AWS_PROFILE") == "test-profile"
        finally:
            # Restore original AWS_PROFILE
            if original_profile:
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
