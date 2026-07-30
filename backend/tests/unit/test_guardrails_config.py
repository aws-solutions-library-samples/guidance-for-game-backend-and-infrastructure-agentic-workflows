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
        """Model should pass FLAT guardrail params that strands BedrockModel accepts.

        Regression guard: strands expects guardrail_id / guardrail_version, NOT a
        nested guardrail_config dict (which strands silently drops with only a
        warning, leaving guardrails disabled). See issue #135.
        """
        reset_model_cache()  # Ensure fresh cache for this test
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-guardrail-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_VERSION", "7"):
                with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", True):
                    with patch("models.cached_bedrock.BedrockModel") as mock_model:
                        # Local modules
                        from models.cached_bedrock import create_cached_bedrock_model

                        create_cached_bedrock_model()

                        call_kwargs = mock_model.call_args[1]
                        # Flat params strands actually honors
                        assert call_kwargs["guardrail_id"] == "test-guardrail-id"
                        assert call_kwargs["guardrail_version"] == "7"
                        # The nested form must never come back (it silently disables guardrails)
                        assert "guardrail_config" not in call_kwargs

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

    def test_guardrails_evaluate_only_latest_message(self):
        """All guardrail-enabled models must set guardrail_latest_message=True.

        Regression guard for issue #201: without this flag, strands attaches
        guardrailConfig to the full Converse request, so input policies
        (PROMPT_ATTACK at HIGH strength) evaluate the entire AgentCore-Memory
        restored conversation. Replayed assistant turns then trigger LOW-
        confidence false positives that block valid in-domain follow-ups.
        """
        reset_model_cache()
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-guardrail-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_VERSION", "7"):
                with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", True):
                    with patch("models.cached_bedrock.BedrockModel") as mock_model:
                        # Local modules
                        from models.cached_bedrock import create_cached_bedrock_model

                        create_cached_bedrock_model()

                        call_kwargs = mock_model.call_args[1]
                        assert call_kwargs["guardrail_latest_message"] is True

    def test_guardrails_latest_message_on_fallback_model(self):
        """The fallback model path must also set guardrail_latest_message=True (issue #201)."""
        reset_model_cache()
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-guardrail-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_VERSION", "7"):
                with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", True):
                    with patch("models.cached_bedrock.BedrockModel") as mock_model:
                        # First construction (primary) raises → fallback path constructs second model
                        mock_model.side_effect = [Exception("primary unavailable"), MagicMock()]
                        # Local modules
                        from models.cached_bedrock import create_cached_bedrock_model

                        create_cached_bedrock_model()

                        assert mock_model.call_count == 2
                        fallback_kwargs = mock_model.call_args_list[1][1]
                        assert fallback_kwargs["guardrail_latest_message"] is True

    def test_guardrails_latest_message_on_override_models(self):
        """Per-agent override models must also set guardrail_latest_message=True (issue #201)."""
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-guardrail-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_VERSION", "7"):
                with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", True):
                    with patch("models.cached_bedrock.BedrockModel") as mock_model:
                        # Local modules
                        from models.cached_bedrock import create_bedrock_model_with_overrides

                        create_bedrock_model_with_overrides(temperature=0.3, max_tokens=2048)

                        call_kwargs = mock_model.call_args[1]
                        assert call_kwargs["guardrail_latest_message"] is True

    def test_guardrails_disabled_via_flag(self):
        """Guardrails should be skipped when disabled via flag."""
        reset_model_cache()  # Ensure fresh cache for this test
        with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ID", "test-id"):
            with patch("models.cached_bedrock.BEDROCK_GUARDRAIL_ENABLED", False):
                with patch("models.cached_bedrock.BedrockModel") as mock_model:
                    # Local modules
                    from models.cached_bedrock import create_cached_bedrock_model

                    create_cached_bedrock_model()

                    # Verify no guardrail params were included
                    call_kwargs = mock_model.call_args[1]
                    assert "guardrail_id" not in call_kwargs
                    assert "guardrail_config" not in call_kwargs
