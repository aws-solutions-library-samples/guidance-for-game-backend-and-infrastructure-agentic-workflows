"""
Inline-chart contract for the trusted backend prompt path (issue #255).

The chat frontend renders a fenced ```chart block (a single JSON object) as an
inline SVG chart when — and only when — the payload validates against the chart
contract in ``ui/src/components/charts/chartContract.ts``. That validator fails
closed: anything out of contract is shown as inert text, never executed.

For that capability to actually reach the model, the contract has to be part of
the **trusted, deployed system prompt** sent to Amazon Bedrock AgentCore — not
only the client-side CopilotChat ``instructions`` prop (which is presentation
config a browser can alter and which never reaches the server-side agent). This
module owns:

  * ``CHART_DIRECTIVE`` — the versioned instruction appended to the orchestrator
    and specialist system prompts (see ``optimized_prompts.py``).
  * ``render_chart_fence`` — a deterministic, fail-closed builder that turns a
    chart spec dict into a ```chart fence, used by the deterministic Cost
    Explorer report so exact, validated figures are charted without routing the
    numbers back through a model.

The bounds below mirror the frontend contract exactly; they are the single
source of truth shared by producer (backend) and consumer (frontend). Keep them
in sync with ``chartContract.ts`` and ``docs/CHART_CONTRACT.md``.
"""

from __future__ import annotations

# Standard library
import json
import math
from dataclasses import dataclass

CHART_CONTRACT_VERSION = "1.0"

# Bounds — mirror ui/src/components/charts/chartContract.ts. Every string length
# (field bounds AND the raw payload cap) is measured in Unicode CODE POINTS, the
# single length metric shared with the frontend, using a strict ``>`` boundary.
MAX_POINTS = 366
MAX_SERIES = 8
MAX_LABEL_LENGTH = 120
MAX_TEXT_LENGTH = 280
MAX_ABS_VALUE = 1e12
MAX_TOTAL_VALUES = 2000
MAX_RAW_PAYLOAD_CHARS = 24_000

CHART_TYPES = frozenset({"bar", "line", "area"})


@dataclass(frozen=True)
class VersionedDirective:
    """Immutable prompt fragment with version metadata (traceability)."""

    name: str
    version: str
    text: str

    def __str__(self) -> str:
        return self.text


# The instruction is deliberately compact (prompts are token-sensitive) but
# complete: it pins the version, the exact shape, the small set of types, and
# the "keep the one-line reading in prose too" rule that keeps the answer
# legible when the SVG is not rendered.
CHART_DIRECTIVE = VersionedDirective(
    name="chart_directive",
    version=CHART_CONTRACT_VERSION,
    text=(
        "CHARTS: When the answer is quantitative — a change over time or a comparison "
        "across items — render a chart inline in addition to (never instead of) a "
        "one-line text reading of what it shows. Emit a fenced code block tagged "
        "`chart` whose body is a single JSON object matching this versioned contract:\n"
        '{"type":"line|bar|area","version":"' + CHART_CONTRACT_VERSION + '",'
        '"title":"...","summary":"one-line reading","unit":"USD|%|<short unit>",'
        '"x":{"label":"Month","values":["Jun","Jul","Aug"]},'
        '"y":{"label":"USD"},'
        '"series":[{"name":"GameLift","values":[1204,1521,1880]}]}\n'
        'Use "line" for trends over time, "bar" for comparison across fleets/items '
        '(multiple series render as grouped bars), and "area" for stacked composition '
        'over time (area values must be non-negative). Every series\' "values" array '
        "MUST have the same length as x.values, all values MUST be finite numbers, and "
        "no extra fields are allowed. Keep the one-line summary in the prose as well so "
        "it survives copy-paste and is available to screen readers."
    ),
)


def with_chart_directive(prompt: str) -> str:
    """Append the versioned chart directive to a system prompt.

    Used by the prompt accessors so the contract is part of the trusted prompt
    text sent to AgentCore for the orchestrator and specialists.
    """
    return f"{prompt}\n\n{CHART_DIRECTIVE.text}"


def _is_renderable_number(value: object) -> bool:
    """True for a finite real number within the shared magnitude bound.

    Integers are compared **directly** against the bound. A Python ``int`` is
    arbitrary precision and ``float(huge_int)`` raises ``OverflowError``, so we
    never convert an int to float; comparing an int to the float bound is exact
    in CPython and cannot overflow (issue #255, finding 2). Floats are checked
    with ``math.isfinite`` (rejecting ``NaN``/``inf``) before the magnitude
    check. ``bool`` is a subclass of ``int`` but is not a chart value here.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        # abs() on a huge int stays an int; the int-vs-float comparison is exact.
        return abs(value) <= MAX_ABS_VALUE
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= MAX_ABS_VALUE
    return False


def _is_bounded_str(value: object, max_length: int) -> bool:
    """True for a string whose length is within ``max_length``.

    Length is measured in Unicode code points — Python ``len`` counts code
    points — which is the single length metric shared with the frontend (see
    ``chartContract.ts``). An astral (non-BMP) character therefore counts as 1
    on both sides, so the same string is accepted or rejected identically.
    """
    return isinstance(value, str) and len(value) <= max_length


def _raw_payload_within_cap(raw: str) -> bool:
    """True when a rendered fenced-block body is within the pre-parse size cap.

    Uses the same code-point length metric and the same strict ``>`` boundary as
    the frontend, so a body of exactly ``MAX_RAW_PAYLOAD_CHARS`` code points is
    allowed and any larger one is refused identically on both sides.
    """
    return isinstance(raw, str) and len(raw) <= MAX_RAW_PAYLOAD_CHARS


def build_chart_spec(spec: dict) -> dict | None:
    """Validate a chart spec dict against the shared contract.

    Returns the spec on success, or ``None`` for any out-of-contract input.
    **Never raises** — it is a fail-closed security validator, so any unexpected
    error is swallowed and treated as a rejection (issue #255, finding 2).
    Mirrors the frontend validator so a backend-produced chart is guaranteed to
    be accepted by the renderer.
    """
    try:
        return _validate_chart_spec(spec)
    except Exception:  # pragma: no cover - defensive: a validator must never raise
        return None


def _validate_chart_spec(spec: object) -> dict | None:
    if not isinstance(spec, dict):
        return None

    allowed = {"type", "version", "title", "summary", "unit", "x", "y", "series"}
    if any(key not in allowed for key in spec):
        return None

    chart_type = spec.get("type")
    if chart_type not in CHART_TYPES:
        return None

    # Optional fields: reject a present-but-null value (parity with the frontend,
    # which rejects an explicit ``null`` for every optional field). An ABSENT key
    # is fine; a present ``None`` is not, so ``"key" in spec`` — not ``.get()`` —
    # is the correct test (issue #255, finding 1).
    if "version" in spec:
        version = spec["version"]
        if not _is_bounded_str(version, MAX_LABEL_LENGTH):
            return None
        if version.split(".", 1)[0] != CHART_CONTRACT_VERSION.split(".", 1)[0]:
            return None

    for key, max_length in (("title", MAX_TEXT_LENGTH), ("summary", MAX_TEXT_LENGTH), ("unit", MAX_LABEL_LENGTH)):
        if key in spec and not _is_bounded_str(spec[key], max_length):
            return None

    x = spec.get("x")
    if not isinstance(x, dict) or any(key not in {"label", "values"} for key in x):
        return None
    if "label" in x and not _is_bounded_str(x["label"], MAX_LABEL_LENGTH):
        return None
    x_values = x.get("values")
    if not isinstance(x_values, list) or not 0 < len(x_values) <= MAX_POINTS:
        return None
    if not all(_is_bounded_str(label, MAX_LABEL_LENGTH) for label in x_values):
        return None
    point_count = len(x_values)

    if "y" in spec:
        y = spec["y"]
        if not isinstance(y, dict) or any(key not in {"label"} for key in y):
            return None
        if "label" in y and not _is_bounded_str(y["label"], MAX_LABEL_LENGTH):
            return None

    series = spec.get("series")
    if not isinstance(series, list) or not 0 < len(series) <= MAX_SERIES:
        return None
    if len(series) * point_count > MAX_TOTAL_VALUES:
        return None

    reject_negative = chart_type == "area"
    for candidate in series:
        if not isinstance(candidate, dict) or any(key not in {"name", "values"} for key in candidate):
            return None
        if not _is_bounded_str(candidate.get("name"), MAX_LABEL_LENGTH):
            return None
        values = candidate.get("values")
        if not isinstance(values, list) or len(values) != point_count:
            return None
        if not all(_is_renderable_number(v) for v in values):
            return None
        # Values are ints/floats here; the comparison never float-converts.
        if reject_negative and any(v < 0 for v in values):
            return None

    return spec


def render_chart_fence(spec: dict) -> str:
    """Render a contract-valid ```chart fenced block, or "" if the spec is invalid.

    Fail-closed: an invalid spec (or any unexpected error) returns an empty
    string so the caller simply omits the chart rather than emitting something
    the frontend would reject. **Never raises** (issue #255, finding 2).
    """
    try:
        validated = build_chart_spec(spec)
        if validated is None:
            return ""
        body = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
        if not _raw_payload_within_cap(body):
            return ""
        return f"```chart\n{body}\n```"
    except Exception:  # pragma: no cover - defensive: rendering must never raise
        return ""
