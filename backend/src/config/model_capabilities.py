"""Per-model inference capabilities for Bedrock Converse requests.

Different Claude model generations accept different inference parameters. The
Claude 5.5 models reject the ``temperature`` and ``top_p`` sampling parameters
and run adaptive thinking unless an explicit ``thinking`` field turns it off,
while earlier Claude models accept ``temperature`` and take no ``thinking``
field. This module keeps that knowledge in one small, explicit table so the
model-construction path can decide, per model:

* whether to send ``temperature`` in the Converse ``inferenceConfig``, and
* which ``thinking`` value to send through the Strands ``BedrockModel``
  ``additional_request_fields`` (surfaced as ``additionalModelRequestFields``).

The table is keyed by substrings of the model ID so it matches foundation
model IDs and the ``us.``/``global.`` cross-region inference profile prefixes
alike. Application inference profile IDs are opaque ARNs that cannot be matched
by pattern; those fall back to the earlier-generation behavior (keep
``temperature``, no ``thinking`` field) and emit a one-time warning so the
operator can confirm the model behind the profile accepts that combination.

#421 tracks the longer-term capability-aware model factory that would replace
this table with provider-reported capabilities.
"""

# Standard library
from dataclasses import dataclass

# Third-party packages
from loguru import logger


@dataclass(frozen=True)
class ModelCapabilities:
    """How to shape an inference request for one model generation.

    Attributes:
        send_temperature: Whether ``temperature`` is accepted and should be
            sent in ``inferenceConfig``. Models that reject the parameter set
            this to ``False`` so it is omitted entirely.
        thinking: The value for the ``thinking`` request field, or ``None`` to
            omit the field. Sent through Strands ``additional_request_fields``.
    """

    send_temperature: bool
    thinking: dict | None = None


# Earlier Claude generations: temperature is accepted and no thinking field is
# sent. This also serves as the fallback for IDs the table cannot match.
_LEGACY_CLAUDE = ModelCapabilities(send_temperature=True, thinking=None)

# Pattern table, evaluated in order. Each key is matched as a substring of the
# model ID so it covers bare foundation model IDs and the ``us.``/``global.``
# cross-region inference prefixes. Keep the most specific patterns first.
_CAPABILITY_PATTERNS: tuple[tuple[str, ModelCapabilities], ...] = (
    # Claude Haiku 5.5 rejects temperature/top_p and turns thinking off with
    # {"type": "disabled"}.
    ("claude-haiku-5-5", ModelCapabilities(send_temperature=False, thinking={"type": "disabled"})),
    # Claude Sonnet 5.5 rejects temperature/top_p and turns thinking off with
    # {"type": "between_tools"}; it rejects {"type": "disabled"}.
    ("claude-sonnet-5-5", ModelCapabilities(send_temperature=False, thinking={"type": "between_tools"})),
)


def get_model_capabilities(model_id: str) -> ModelCapabilities:
    """Return the inference capabilities for ``model_id``.

    Matching is a case-insensitive substring test against the pattern table so
    it works for foundation model IDs and ``us.``/``global.`` inference profile
    prefixes. An ID that matches nothing - including an application inference
    profile ARN - falls back to the earlier-generation behavior and logs a
    one-time warning so the operator can confirm that model accepts a
    ``temperature`` parameter with no ``thinking`` field.
    """
    normalized = model_id.lower()
    for pattern, capabilities in _CAPABILITY_PATTERNS:
        if pattern in normalized:
            return capabilities

    logger.warning(
        f"No inference-capability entry matched model ID '{model_id}'; "
        "sending temperature with no thinking field (earlier-generation behavior). "
        "If this is an application inference profile or a newer model, confirm it accepts "
        "that combination."
    )
    return _LEGACY_CLAUDE
