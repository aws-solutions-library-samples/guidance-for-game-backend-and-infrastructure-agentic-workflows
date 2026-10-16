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
alike. It lists both the current default models and the previous default
rollback IDs, so a rollback resolves silently without a warning.

An ID the table cannot match - including an application inference profile
ARN - falls back to the earlier-generation shape (keep ``temperature``, no
``thinking`` field) and emits a warning, logged once per distinct model ID.
That fallback is correct for a genuinely earlier model. An ID that looks like
a Claude 5.x or Opus 4.7 through 4.9 model likely rejects ``temperature`` with
a 400, so the warning says the request will likely fail and points to a listed
model ID or a new capability entry. The runtime cannot tell which model an
application inference profile wraps, so the warning for a profile ARN is
conditional: requests through the profile fail with a 400 only if it wraps a
model that rejects ``temperature``. For Claude Haiku 5.5 or Claude Sonnet 5.5
the ``global.`` system inference profile ID resolves the model directly; for
any other such model the remedy is its model ID plus a capability entry.
Application inference profiles over the 5.5 models stay unsupported until #421
lands the capability-aware model factory, which can resolve the model a
profile wraps.

#421 tracks the longer-term capability-aware model factory that would replace
this table with provider-reported capabilities.
"""

# Standard library
import re
import threading
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

# Model IDs that have already produced a fallback warning. The lookup runs on
# every model construction, so this keeps the unmatched-ID warning to one line
# per distinct ID instead of one per request.
_warned_model_ids: set[str] = set()
_warned_lock = threading.Lock()

# Pattern table, evaluated in order. Each key is matched as a substring of the
# model ID so it covers bare foundation model IDs and the ``us.``/``global.``
# cross-region inference prefixes. Keep the most specific patterns first. The
# previous default rollback IDs are listed explicitly so a rollback resolves
# without the unrecognized-ID warning.
_CAPABILITY_PATTERNS: tuple[tuple[str, ModelCapabilities], ...] = (
    # Claude Haiku 5.5 rejects temperature/top_p and turns thinking off with
    # {"type": "disabled"}.
    ("claude-haiku-5-5", ModelCapabilities(send_temperature=False, thinking={"type": "disabled"})),
    # Claude Sonnet 5.5 rejects temperature/top_p and turns thinking off with
    # {"type": "between_tools"}; it rejects {"type": "disabled"}.
    ("claude-sonnet-5-5", ModelCapabilities(send_temperature=False, thinking={"type": "between_tools"})),
    # Previous default rollback targets. These earlier models accept temperature
    # and take no thinking field; listing them keeps a rollback silent.
    ("claude-haiku-4-5", _LEGACY_CLAUDE),
    ("claude-sonnet-4-6", _LEGACY_CLAUDE),
)

# An unrecognized ID that still looks like a newer Claude generation likely
# rejects temperature. Claude 5.x (any family) and Opus 4.7 through 4.9 likely
# follow the newer parameter rules (documented for Opus 4.7), so the fallback
# shape would likely draw a 400 from them. The warning hedges the prediction
# and names the remedy.
_LIKELY_REJECTS_TEMPERATURE = re.compile(r"claude-(?:haiku|sonnet|opus)-5|opus-4-[7-9]")


def get_model_capabilities(model_id: str) -> ModelCapabilities:
    """Return the inference capabilities for ``model_id``.

    Matching is a case-insensitive substring test against the pattern table so
    it works for foundation model IDs and ``us.``/``global.`` inference profile
    prefixes. The table lists the current default models and the previous
    default rollback IDs, so both resolve without a warning.

    An ID that matches nothing falls back to the earlier-generation behavior
    (send ``temperature``, no ``thinking`` field) and logs a warning once per
    distinct ID. That fallback is correct for a genuinely earlier model. When
    the unrecognized ID looks like a Claude 5.x or Opus 4.7 through 4.9 model,
    the fallback likely sends ``temperature`` to a model that rejects it and the
    request likely draws a 400; the warning says so and points to a listed model
    ID or a new capability entry. For an application inference profile ARN the
    warning is conditional, because the runtime cannot tell which model the
    profile wraps.
    """
    normalized = model_id.lower()
    for pattern, capabilities in _CAPABILITY_PATTERNS:
        if pattern in normalized:
            return capabilities

    _warn_unmatched_once(model_id, normalized)
    return _LEGACY_CLAUDE


def _warn_unmatched_once(model_id: str, normalized: str) -> None:
    """Log the unmatched-ID fallback warning once per distinct model ID.

    The message states the cause: an unrecognized ID gets the
    earlier-generation shape, which sends a ``temperature`` parameter. For an
    ID that looks like a Claude 5.x or Opus 4.7 through 4.9 model, that
    parameter is likely rejected with a 400, and the remedy is a listed model
    ID or a new capability entry. For an application inference profile ARN the
    warning is conditional, because the runtime cannot tell which model the
    profile wraps.
    """
    with _warned_lock:
        if model_id in _warned_model_ids:
            return
        _warned_model_ids.add(model_id)

    is_profile = "application-inference-profile" in normalized

    message = (
        f"No inference-capability entry matched model ID '{model_id}'; "
        "sending temperature with no thinking field (earlier-generation behavior)."
    )
    if is_profile:
        message += (
            " The runtime cannot tell which model an application inference profile "
            "wraps. If it wraps a model that rejects the temperature parameter, every "
            "request fails with a 400. For Claude Haiku 5.5 or Claude Sonnet 5.5, use "
            "the 'global.' system inference profile ID instead (application inference "
            "profiles over the 5.5 models are unsupported until #421); for any other "
            "such model, use its model ID and add a capability entry for it."
        )
    elif _LIKELY_REJECTS_TEMPERATURE.search(normalized):
        message += (
            " This ID looks like a Claude 5.x or Opus 4.7 through 4.9 model, which "
            "likely rejects the temperature parameter, so the request will likely fail "
            "with a 400. Use a model ID listed in config/model_capabilities.py or add a "
            "capability entry for this model."
        )
    else:
        message += (
            " This is correct for a genuinely earlier model. If it is instead a newer "
            "model that rejects temperature, the request fails with a 400; use its "
            "model ID and add a capability entry for it."
        )

    logger.warning(message)


def reset_capability_warnings() -> None:
    """Clear the set of model IDs that have warned. For tests only."""
    with _warned_lock:
        _warned_model_ids.clear()
