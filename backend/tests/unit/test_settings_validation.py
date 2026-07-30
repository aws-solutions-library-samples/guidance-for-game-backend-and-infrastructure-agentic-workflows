#!/usr/bin/env python3
"""Unit tests for settings validation and role-based model resolution."""

# Standard library
import importlib
import os
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
from config.model_settings import DEFAULT_ORCHESTRATOR_MODEL_ID, DEFAULT_SPECIALIST_MODEL_ID, resolve_model_ids

pytestmark = pytest.mark.unit


class TestSettingsValidation:
    """Test general settings validation and error handling."""

    def test_aws_region_default(self):
        # Local modules
        import config.settings

        importlib.reload(config.settings)
        assert config.settings.AWS_REGION == os.getenv("AWS_REGION", "us-west-2")

    @patch.dict(os.environ, {"AWS_REGION": "us-east-1"})
    def test_aws_region_override(self):
        # Local modules
        import config.settings

        importlib.reload(config.settings)
        assert config.settings.AWS_REGION == "us-east-1"

    def test_memory_settings_defaults(self):
        # Local modules
        import config.settings

        importlib.reload(config.settings)
        assert config.settings.USE_BEDROCK_SESSIONS is True
        assert config.settings.MEMORY_SESSION_TTL_HOURS == 24
        assert config.settings.MEMORY_USER_TTL_DAYS == 30
        assert isinstance(config.settings.MEMORY_LONG_TERM_ENABLED, bool)

    @patch.dict(os.environ, {"GBAW_MEMORY_LONG_TERM_ENABLED": "true"})
    def test_memory_long_term_enabled(self):
        # Local modules
        import config.settings

        importlib.reload(config.settings)
        assert config.settings.MEMORY_LONG_TERM_ENABLED is True


class TestRoleModelResolution:
    """Canonical role variables override aliases, which override defaults."""

    def test_defaults(self):
        assert resolve_model_ids({}) == (DEFAULT_ORCHESTRATOR_MODEL_ID, DEFAULT_SPECIALIST_MODEL_ID)

    def test_canonical_role_overrides(self):
        assert resolve_model_ids(
            {
                "GBAW_ORCHESTRATOR_MODEL_ID": "orchestrator-custom",
                "GBAW_SPECIALIST_MODEL_ID": "specialist-custom",
            }
        ) == ("orchestrator-custom", "specialist-custom")

    def test_legacy_aliases(self):
        assert resolve_model_ids(
            {
                "GBAW_BEDROCK_MODEL_ID": "legacy-orchestrator",
                "GBAW_BEDROCK_MODEL_ID_SECONDARY": "legacy-specialist",
            }
        ) == ("legacy-orchestrator", "legacy-specialist")

    def test_canonical_variables_take_precedence_over_aliases(self):
        assert resolve_model_ids(
            {
                "GBAW_ORCHESTRATOR_MODEL_ID": "canonical-orchestrator",
                "GBAW_SPECIALIST_MODEL_ID": "canonical-specialist",
                "GBAW_BEDROCK_MODEL_ID": "legacy-orchestrator",
                "GBAW_BEDROCK_MODEL_ID_SECONDARY": "legacy-specialist",
            }
        ) == ("canonical-orchestrator", "canonical-specialist")

    def test_empty_values_are_unset(self):
        assert resolve_model_ids(
            {
                "GBAW_ORCHESTRATOR_MODEL_ID": "",
                "GBAW_SPECIALIST_MODEL_ID": "",
                "GBAW_BEDROCK_MODEL_ID": "legacy-orchestrator",
                "GBAW_BEDROCK_MODEL_ID_SECONDARY": "legacy-specialist",
            }
        ) == ("legacy-orchestrator", "legacy-specialist")
