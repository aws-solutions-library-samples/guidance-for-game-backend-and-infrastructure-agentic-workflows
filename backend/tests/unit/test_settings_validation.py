#!/usr/bin/env python3
"""Unit tests for settings validation and error handling."""

# Standard library
import os
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


class TestSettingsValidation:
    """Test settings validation and error handling."""

    def test_aws_region_default(self):
        """Test that AWS_REGION has correct default."""
        # Standard library
        import importlib

        # Local modules
        import config.settings

        importlib.reload(config.settings)
        # Local modules
        from config.settings import AWS_REGION

        # Should default to us-west-2
        assert AWS_REGION == "us-west-2"

    @patch.dict(os.environ, {"AWS_REGION": "us-east-1"})
    def test_aws_region_override(self):
        """Test that AWS_REGION can be overridden."""
        # Standard library
        import importlib

        # Local modules
        import config.settings

        importlib.reload(config.settings)
        # Local modules
        from config.settings import AWS_REGION

        assert AWS_REGION == "us-east-1"

    def test_bedrock_model_default(self):
        """Test that BEDROCK_MODEL_ID has correct default."""
        # Standard library
        import importlib
        import os

        # Clear environment variable to test default
        old_value = os.environ.pop("GBAW_BEDROCK_MODEL_ID", None)

        try:
            # Local modules
            import config.settings

            importlib.reload(config.settings)
            # Local modules
            from config.settings import BEDROCK_MODEL_ID

            assert BEDROCK_MODEL_ID == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        finally:
            # Restore original value
            if old_value:
                os.environ["GBAW_BEDROCK_MODEL_ID"] = old_value

    def test_memory_settings_defaults(self):
        """Test that memory settings have correct defaults."""
        # Standard library
        import importlib

        # Local modules
        import config.settings

        importlib.reload(config.settings)
        # Local modules
        from config.settings import (
            MEMORY_LONG_TERM_ENABLED,
            MEMORY_SESSION_TTL_HOURS,
            MEMORY_USER_TTL_DAYS,
            USE_BEDROCK_SESSIONS,
        )

        assert USE_BEDROCK_SESSIONS == True  # Default enabled
        assert MEMORY_SESSION_TTL_HOURS == 24
        assert MEMORY_USER_TTL_DAYS == 30
        # Memory long-term setting depends on environment configuration
        assert isinstance(MEMORY_LONG_TERM_ENABLED, bool)  # Should be boolean

    @patch.dict(os.environ, {"GBAW_MEMORY_LONG_TERM_ENABLED": "true"})
    def test_memory_long_term_enabled(self):
        """Test that MEMORY_LONG_TERM_ENABLED can be enabled."""
        # Standard library
        import importlib

        # Local modules
        import config.settings

        importlib.reload(config.settings)
        # Local modules
        from config.settings import MEMORY_LONG_TERM_ENABLED

        assert MEMORY_LONG_TERM_ENABLED == True
