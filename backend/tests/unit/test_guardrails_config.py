"""
Unit tests for Bedrock Guardrails configuration.
"""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from models.cached_bedrock import reset_model_cache

pytestmark = pytest.mark.unit


class TestGuardrailsConfiguration:
    """Test guardrails configuration and integration."""

    def setup_method(self):
        """Reset model cache before each test."""
        reset_model_cache()

    def test_guardrails_settings_loaded(self):
        """Guardrail settings should be loaded from environment."""
        # Local modules
        from config.settings import BEDROCK_GUARDRAIL_ENABLED, BEDROCK_GUARDRAIL_ID, BEDROCK_GUARDRAIL_VERSION

        # Settings should exist (may be None if not configured)
        assert BEDROCK_GUARDRAIL_VERSION is not None
        assert isinstance(BEDROCK_GUARDRAIL_ENABLED, bool)

    def test_model_includes_guardrails_when_configured(self):
        """Model should include guardrails when ID is configured."""
        reset_model_cache()  # Ensure fresh cache for this test
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-guardrail-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", True):
                with patch("models.cached_bedrock.BedrockModel") as mock_model:
                    # Local modules
                    from models.cached_bedrock import create_cached_bedrock_model

                    create_cached_bedrock_model()

                    # Verify BedrockModel was called with guardrail_config
                    call_kwargs = mock_model.call_args[1]
                    assert "guardrail_config" in call_kwargs
                    assert call_kwargs["guardrail_config"]["guardrailIdentifier"] == "test-guardrail-id"

    def test_model_works_without_guardrails(self):
        """Model should work when guardrails are not configured."""
        reset_model_cache()  # Ensure fresh cache for this test
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", None):
            with patch("models.cached_bedrock.BedrockModel") as mock_model:
                # Local modules
                from models.cached_bedrock import create_cached_bedrock_model

                mock_model.return_value = MagicMock()
                model = create_cached_bedrock_model()

                # Should still create model
                assert model is not None
                mock_model.assert_called_once()

    def test_guardrails_disabled_via_flag(self):
        """Guardrails should be skipped when disabled via flag."""
        reset_model_cache()  # Ensure fresh cache for this test
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", False):
                with patch("models.cached_bedrock.BedrockModel") as mock_model:
                    # Local modules
                    from models.cached_bedrock import create_cached_bedrock_model

                    create_cached_bedrock_model()

                    # Verify guardrail_config was NOT included
                    call_kwargs = mock_model.call_args[1]
                    assert "guardrail_config" not in call_kwargs
