#!/usr/bin/env python3
"""Example tests: Source Control specialist prompt + inference wiring.

These are structural / wiring guarantees (example, non-property tests) for Task 8.1's
specialist integration — no AWS, model, or network calls occur.

Three independent wiring behaviors are asserted here:

1. **Prompt registration + code fallback (Req 12.1).** The Source Control specialist prompt
   is registered in ``optimized_prompts`` (present in both ``_ALL_PROMPTS`` and
   ``get_prompt_versions()``), and ``get_optimized_source_control_prompt()`` returns the
   non-empty code-defined ``SOURCE_CONTROL_PROMPT`` text when no Bedrock Prompt Management
   runtime prompt is available — mirroring the gamelift/eks/cost specialists.

2. **Bedrock Prompt Management ARN lookup (Req 12.1).** The ``_load_from_bedrock_pm`` ARN map
   is wired to consult ``GBAW_SOURCE_CONTROL_PROMPT_ARN`` under the ``source_control_specialist``
   key, so a deployed managed prompt overrides the code fallback.

3. **Inference configuration (Req 12.2).** ``INFERENCE_CONFIG`` contains a ``"sourcecontrol"``
   entry carrying ``model_id`` + ``temperature`` + ``max_tokens``, and the specialist's
   ``service_name="SourceControl"`` lowercases to that lookup key (the resolution
   ``create_specialist_agent`` performs via ``INFERENCE_CONFIG.get(service_name.lower())``).

Validates: Requirements 12.1, 12.2
"""

# Standard library
import inspect

# Third-party packages
import pytest

# Local modules
import agents.optimized_prompts as optimized_prompts
from config.settings import INFERENCE_CONFIG

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Behavior 1: prompt registration + code fallback (Req 12.1)
# ---------------------------------------------------------------------------


def test_source_control_prompt_is_registered():
    """The specialist prompt is registered in the prompt registry (Req 12.1)."""
    assert "source_control_specialist" in optimized_prompts._ALL_PROMPTS

    entry = optimized_prompts._ALL_PROMPTS["source_control_specialist"]
    assert entry.name == "source_control_specialist"

    # Exposed for logging/tracing alongside the other specialists.
    versions = optimized_prompts.get_prompt_versions()
    assert "source_control_specialist" in versions
    assert versions["source_control_specialist"] == entry.version


def test_source_control_prompt_has_code_fallback(monkeypatch):
    """With no runtime (managed) prompt available, the code fallback text is returned (Req 12.1)."""
    # Force the "no Bedrock PM prompt loaded" state so the code fallback path is exercised.
    monkeypatch.setattr(optimized_prompts, "_runtime_prompts", {})

    text = optimized_prompts.get_optimized_source_control_prompt()

    assert isinstance(text, str)
    assert text.strip(), "code fallback prompt must be non-empty"
    assert text == optimized_prompts.SOURCE_CONTROL_PROMPT.text


def test_runtime_prompt_overrides_code_fallback(monkeypatch):
    """Positive control: a loaded managed prompt overrides the code fallback (Req 12.1)."""
    monkeypatch.setattr(
        optimized_prompts,
        "_runtime_prompts",
        {"source_control_specialist": "managed prompt text"},
    )

    assert optimized_prompts.get_optimized_source_control_prompt() == "managed prompt text"


# ---------------------------------------------------------------------------
# Behavior 2: Bedrock Prompt Management ARN lookup (Req 12.1)
# ---------------------------------------------------------------------------


def test_bedrock_pm_arn_lookup_consults_source_control_env_var(monkeypatch):
    """``_load_from_bedrock_pm`` consults ``GBAW_SOURCE_CONTROL_PROMPT_ARN`` (Req 12.1).

    We record the env-var names the loader reads. Returning ``None`` for every ARN makes the
    loader early-return (``not any(arn_map.values())``) before any boto3 call, so this stays a
    fast, offline example test while still proving the mapping is wired.
    """
    consulted: list[str] = []

    def _recording_getenv(name, default=None):
        consulted.append(name)
        return None

    monkeypatch.setattr(optimized_prompts.os, "getenv", _recording_getenv)

    optimized_prompts._load_from_bedrock_pm()

    assert "GBAW_SOURCE_CONTROL_PROMPT_ARN" in consulted


def test_arn_map_maps_specialist_key_to_source_control_env_var():
    """The ARN map ties ``source_control_specialist`` → ``GBAW_SOURCE_CONTROL_PROMPT_ARN`` (Req 12.1)."""
    src = inspect.getsource(optimized_prompts._load_from_bedrock_pm)
    assert '"source_control_specialist": os.getenv("GBAW_SOURCE_CONTROL_PROMPT_ARN")' in src


# ---------------------------------------------------------------------------
# Behavior 3: inference configuration (Req 12.2)
# ---------------------------------------------------------------------------


def test_inference_config_has_source_control_entry():
    """``INFERENCE_CONFIG['sourcecontrol']`` exists with the required fields (Req 12.2)."""
    assert "sourcecontrol" in INFERENCE_CONFIG

    cfg = INFERENCE_CONFIG["sourcecontrol"]
    assert cfg["model_id"]
    assert isinstance(cfg["temperature"], (int, float))
    assert isinstance(cfg["max_tokens"], int)
    assert cfg["max_tokens"] > 0


def test_service_name_lowercases_to_inference_lookup_key():
    """``service_name='SourceControl'`` resolves to the ``sourcecontrol`` inference entry (Req 12.2)."""
    assert "SourceControl".lower() == "sourcecontrol"
    assert INFERENCE_CONFIG.get("SourceControl".lower()) is INFERENCE_CONFIG["sourcecontrol"]
