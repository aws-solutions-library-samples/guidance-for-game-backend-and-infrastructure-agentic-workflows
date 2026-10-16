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
alike. It lists both the current default models and the documented
earlier-generation rollback IDs, so a rollback resolves silently without a
warning.

An ID the table cannot match - including an opaque application inference
profile ARN - falls back to the earlier-generation shape (keep
``temperature``, no ``thinking`` field) and emits a warning, logged once per
distinct model ID. That fallback is only safe for a genuinely earlier model.
Claude 5.x and Opus 4.7+ models (and application inference profiles that wrap
one) reject ``temperature`` with a 400, so the warning states that an
unrecognized newer model or such a profile will be rejected and points to the
listed model IDs or the ``global.`` system inference profile ID. Application
inference profiles over the 5.5 models stay unsupported until #421 lands the
capability-aware model factory, which can resolve the model a profile wraps.

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
# documented earlier-generation rollback IDs are listed explicitly so a
# rollback resolves without the unrecognized-ID warning.
_CAPABILITY_PATTERNS: tuple[tuple[str, ModelCapabilities], ...] = (
    # Claude Haiku 5.5 rejects temperature/top_p and turns thinking off with
    # {"type": "disabled"}.
    ("claude-haiku-5-5", ModelCapabilities(send_temperature=False, thinking={"type": "disabled"})),
    # Claude Sonnet 5.5 rejects temperature/top_p and turns thinking off with
    # {"type": "between_tools"}; it rejects {"type": "disabled"}.
    ("claude-sonnet-5-5", ModelCapabilities(send_temperature=False, thinking={"type": "between_tools"})),
    # Documented rollback targets. These earlier models accept temperature and
    # take no thinking field; listing them keeps a rollback silent.
    ("claude-haiku-4-5", _LEGACY_CLAUDE),
    ("claude-sonnet-4-6", _LEGACY_CLAUDE),
)

# An unrecognized ID that still looks like a newer Claude generation most likely
# rejects temperature. Claude 5.x (any family) and Opus 4.7 through 4.9 take the
# newer parameter rules, so the fallback shape would draw a 400 from them. The
# warning calls this out instead of implying the fallback is always safe.
_LIKELY_REJECTS_TEMPERATURE = re.compile(r"claude-(?:haiku|sonnet|opus)-5|opus-4-[7-9]")


def get_model_capabilities(model_id: str) -> ModelCapabilities:
    """Return the inference capabilities for ``model_id``.

    Matching is a case-insensitive substring test against the pattern table so
    it works for foundation model IDs and ``us.``/``global.`` inference profile
    prefixes. The table lists the current default models and the documented
    earlier-generation rollback IDs, so both resolve without a warning.

    An ID that matches nothing falls back to the earlier-generation behavior
    (send ``temperature``, no ``thinking`` field) and logs a warning once per
    distinct ID. That fallback is only correct for a genuinely earlier model.
    When the unrecognized ID still looks like a Claude 5.x or Opus 4.7+ model -
    or is an opaque application inference profile ARN, which cannot be matched
    by pattern - the fallback sends ``temperature`` to a model that rejects it
    and the request draws a 400. The warning states that explicitly and points
    to a listed model ID or the ``global.`` system inference profile ID.
    """
    normalized = model_id.lower()
    for pattern, capabilities in _CAPABILITY_PATTERNS:
        if pattern in normalized:
            return capabilities

    _warn_unmatched_once(model_id, normalized)
    return _LEGACY_CLAUDE


def _warn_unmatched_once(model_id: str, normalized: str) -> None:
    """Log the unmatched-ID fallback warning once per distinct model ID.

    The message names the real cause: an unrecognized ID gets the
    earlier-generation shape, which a newer model (Claude 5.x, Opus 4.7+) or an
    application inference profile wrapping one will reject because it receives a
    ``temperature`` parameter.
    """
    with _warned_lock:
        if model_id in _warned_model_ids:
            return
        _warned_model_ids.add(model_id)

    is_profile = "application-inference-profile" in normalized
    likely_rejects = is_profile or bool(_LIKELY_REJECTS_TEMPERATURE.search(normalized))

    message = (
        f"No inference-capability entry matched model ID '{model_id}'; "
        "sending temperature with no thinking field (earlier-generation behavior)."
    )
    if likely_rejects:
        message += (
            " This ID looks like a Claude 5.x or Opus 4.7+ model or an application "
            "inference profile wrapping one, which rejects the temperature parameter, "
            "so requests will fail with a 400. Use a listed foundation model ID or the "
            "'global.' system inference profile ID instead (application inference "
            "profiles over the 5.5 models are unsupported until #421)."
        )
    else:
        message += (
            " This is correct only for a genuinely earlier Claude model; a newer model "
            "that rejects temperature will fail with a 400."
        )

    logger.warning(message)


def reset_capability_warnings() -> None:
    """Clear the set of model IDs that have warned. For tests only."""
    with _warned_lock:
        _warned_model_ids.clear()
