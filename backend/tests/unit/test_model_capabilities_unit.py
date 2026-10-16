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
import re
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
        # The ``us.`` prefix resolves the same as ``global.``, and so do the
        # other geographic prefixes, a bare foundation ID, and mixed-case input;
        # the table matches a case-insensitive substring.
        for model_id in (
            "us.anthropic.claude-sonnet-5-5",
            "eu.anthropic.claude-sonnet-5-5",
            "au.anthropic.claude-sonnet-5-5",
            "jp.anthropic.claude-sonnet-5-5",
            "anthropic.claude-sonnet-5-5",
            "GLOBAL.ANTHROPIC.CLAUDE-SONNET-5-5",
        ):
            assert get_model_capabilities(model_id).thinking == {"type": "between_tools"}, model_id

    def test_earlier_claude_keeps_temperature_and_no_thinking(self):
        caps = get_model_capabilities("global.anthropic.claude-haiku-4-5-20251001-v1:0")
        assert caps.send_temperature is True
        assert caps.thinking is None

    def test_previous_default_rollback_ids_resolve_silently(self):
        # The previous default rollback IDs are listed in the table, so resolving
        # them keeps temperature, sends no thinking field, and does not warn.
        with patch("config.model_capabilities.logger") as mock_logger:
            haiku = get_model_capabilities("global.anthropic.claude-haiku-4-5-20251001-v1:0")
            sonnet = get_model_capabilities("global.anthropic.claude-sonnet-4-6")
        assert haiku.send_temperature is True and haiku.thinking is None
        assert sonnet.send_temperature is True and sonnet.thinking is None
        mock_logger.warning.assert_not_called()

    def test_unmatched_newer_model_warns_that_it_will_likely_be_rejected(self):
        # An unrecognized ID that still looks like a newer generation (Claude
        # 5.x or Opus 4.7 through 4.9) gets the earlier-generation fallback shape, which
        # that model likely rejects. The warning must name that branch: a
        # hedged 400 prediction and a remedy of a listed model or a new entry,
        # not the profile advice and not the softer earlier-model note.
        for model_id in (
            "global.anthropic.claude-opus-5-5",
            "global.anthropic.claude-opus-4-7",
            "global.anthropic.claude-sonnet-5",
            "global.anthropic.claude-haiku-5-0",
        ):
            reset_capability_warnings()
            with patch("config.model_capabilities.logger") as mock_logger:
                caps = get_model_capabilities(model_id)
            assert caps.send_temperature is True, model_id
            mock_logger.warning.assert_called_once()
            message = mock_logger.warning.call_args.args[0]
            assert "temperature" in message, model_id
            assert "400" in message, model_id
            assert "likely" in message, model_id
            assert "add a capability entry" in message, model_id
            # This branch must not borrow the profile wording.
            assert "application inference profile" not in message, model_id
            # Nor the profile branch's remedy: a newer model is not fixed by a
            # system inference profile ID, so that advice must not reappear here.
            assert "system inference profile" not in message, model_id

    def test_unmatched_earlier_model_warns_softly_without_predicting_rejection(self):
        # An unrecognized ID that does not look like a newer generation gets the
        # soft note: the fallback is correct for a genuinely earlier model. It
        # must not predict that the request will likely fail, so an over-broad
        # classifier that routed it to the reject-400 branch is caught here.
        with patch("config.model_capabilities.logger") as mock_logger:
            caps = get_model_capabilities("global.anthropic.claude-sonnet-4-5-20250929-v1:0")
        assert caps.send_temperature is True
        assert caps.thinking is None
        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args.args[0]
        assert "genuinely earlier" in message
        # The soft branch states the earlier-model case plainly; it must not use
        # the newer-model branch's hedged 400 prediction.
        assert "likely fail" not in message
        assert "likely rejects" not in message
        # It must name the remedy for a misclassified newer model: its model ID
        # plus a capability entry. Reverting the branch to a bare note (dropping
        # the remedy) is caught here.
        assert "add a capability entry" in message
        # The soft branch must not call every unmatched ID a Claude model; the
        # fallback serves any earlier model, so a blanket "Claude model" claim
        # (or an unconditional rejection phrased around one) is caught.
        assert "Claude model" not in message
        # It must not borrow the profile branch's wording.
        assert "application inference profile" not in message
        assert "system inference profile" not in message

    def test_unmatched_profile_falls_back_and_warns_conditionally(self):
        with patch("config.model_capabilities.logger") as mock_logger:
            caps = get_model_capabilities(
                "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abcd1234"
            )
        assert caps.send_temperature is True
        assert caps.thinking is None
        mock_logger.warning.assert_called_once()
        # The runtime cannot know what the profile wraps, so the warning must be
        # conditional ("if it wraps ... that rejects") rather than asserting a
        # certain 400 for every profile, and it points to the system profile ID.
        message = mock_logger.warning.call_args.args[0]
        assert "application inference profile" in message
        assert "cannot tell which model" in message
        assert "if it wraps" in message.lower()
        assert "global." in message
        # The 400 is stated only under the "if it wraps" condition, never as an
        # unconditional outcome, so a mutation that appends a certain 400 is caught.
        assert message.count("400") == 1
        prediction = message.lower().split("if it wraps", 1)[1]
        assert "400" in prediction
        # Every failure or rejection statement must sit inside the "if it wraps"
        # condition: the runtime cannot tell what the profile wraps, so it must
        # not assert that requests fail or are rejected outright. Any sentence
        # that predicts a failure, a rejection, or a 400 must carry the
        # conditional marker, so an added unconditional rejection sentence is
        # caught whatever its wording (with or without the literal "400").
        lowered = message.lower()
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", lowered) if s.strip()]
        failure_terms = ("fail", "reject", "400", "will not work", "unusable")
        for sentence in sentences:
            if any(term in sentence for term in failure_terms):
                assert "if it wraps" in sentence, sentence
        # The ``global.`` system inference profile ID fixes only the two listed
        # 5.5 models. For any other model that rejects temperature, the remedy is
        # its own model ID plus a capability entry, so the message must name that.
        assert "add a capability entry" in message

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
