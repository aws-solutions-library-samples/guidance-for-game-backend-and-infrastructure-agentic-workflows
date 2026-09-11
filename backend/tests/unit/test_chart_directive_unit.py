"""Unit tests for the inline-chart contract on the trusted backend path (#255).

These mirror the fail-closed rules in ui/src/components/charts/chartContract.ts
so a backend-produced chart fence is guaranteed to be accepted by the frontend,
and lock in that the versioned directive reaches the deployed prompts.
"""

# Standard library
import json
from pathlib import Path

# Third-party packages
import pytest

# Local modules
from agents.chart_directive import (
    CHART_CONTRACT_VERSION,
    CHART_DIRECTIVE,
    MAX_ABS_VALUE,
    MAX_LABEL_LENGTH,
    MAX_POINTS,
    MAX_RAW_PAYLOAD_CHARS,
    MAX_SERIES,
    MAX_TEXT_LENGTH,
    MAX_TOTAL_VALUES,
    _raw_payload_within_cap,
    build_chart_spec,
    render_chart_fence,
    with_chart_directive,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared parity corpus (issue #255, finding 1).
#
# docs/chart-contract-parity-corpus.json is consumed by BOTH this test and the
# TypeScript validator test so the two validators are proven to make the exact
# same decision for optional nulls, the Unicode (code-point) length metric,
# huge/out-of-range numbers, and the raw payload size boundary.
# ---------------------------------------------------------------------------

_PARITY_CORPUS_PATH = Path(__file__).resolve().parents[3] / "docs" / "chart-contract-parity-corpus.json"


def _load_parity_corpus() -> dict:
    with _PARITY_CORPUS_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _materialize(node: object) -> object:
    """Expand any ``{"$str": {"char": c, "count": n}}`` directive into a string.

    Mirrors the TypeScript loader so both sides build byte-identical inputs,
    keeping astral characters and exact boundary lengths compact in the corpus.
    """
    if isinstance(node, dict):
        if set(node.keys()) == {"$str"}:
            spec = node["$str"]
            return spec["char"] * spec["count"]
        return {key: _materialize(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_materialize(item) for item in node]
    return node


_PARITY_CORPUS = _load_parity_corpus()


class TestSharedParityCorpus:
    def test_corpus_bounds_match_module_constants(self):
        bounds = _PARITY_CORPUS["bounds"]
        assert bounds["MAX_POINTS"] == MAX_POINTS
        assert bounds["MAX_SERIES"] == MAX_SERIES
        assert bounds["MAX_LABEL_LENGTH"] == MAX_LABEL_LENGTH
        assert bounds["MAX_TEXT_LENGTH"] == MAX_TEXT_LENGTH
        assert bounds["MAX_ABS_VALUE"] == MAX_ABS_VALUE
        assert bounds["MAX_RAW_PAYLOAD_CHARS"] == MAX_RAW_PAYLOAD_CHARS
        assert _PARITY_CORPUS["lengthMetric"] == "unicode-code-points"
        # The astral character must be exactly one code point (two UTF-16 units),
        # otherwise the length-metric cases would not exercise the divergence.
        assert len(_PARITY_CORPUS["astralChar"]) == 1

    @pytest.mark.parametrize("case", _PARITY_CORPUS["specCases"], ids=lambda c: c["name"])
    def test_spec_case_decision_matches_contract(self, case):
        spec = _materialize(case["spec"])
        accepted = build_chart_spec(spec) is not None
        assert accepted is case["accept"], f"parity case '{case['name']}' expected accept={case['accept']}"

    @pytest.mark.parametrize("case", _PARITY_CORPUS["rawCases"], ids=lambda c: c["name"])
    def test_raw_cap_case_decision_matches_contract(self, case):
        raw = case["char"] * case["count"]
        assert _raw_payload_within_cap(raw) is case["withinCap"], f"raw case '{case['name']}'"


class TestNeverRaisesOnHugeIntegers:
    """Finding 2: numeric validation must never float-convert arbitrarily large
    ints (``float(huge_int)`` raises ``OverflowError``), and the validator /
    renderer must never raise — they fail closed instead."""

    @pytest.mark.parametrize(
        "value",
        [
            10**30,  # far beyond MAX_ABS_VALUE but float()-convertible
            -(10**30),
            10**400,  # float() would raise OverflowError
            -(10**400),
        ],
    )
    def test_build_chart_spec_rejects_huge_int_without_raising(self, value):
        spec = {"type": "line", "x": {"values": ["a"]}, "series": [{"name": "s", "values": [value]}]}
        # Must not raise; must reject (out of magnitude bound).
        assert build_chart_spec(spec) is None

    @pytest.mark.parametrize("value", [10**30, -(10**30), 10**400, -(10**400)])
    def test_render_chart_fence_returns_empty_for_huge_int_without_raising(self, value):
        spec = {"type": "bar", "x": {"values": ["a"]}, "series": [{"name": "s", "values": [value]}]}
        assert render_chart_fence(spec) == ""

    def test_accepts_integer_exactly_at_the_magnitude_bound(self):
        at_bound = int(MAX_ABS_VALUE)
        spec = {"type": "bar", "x": {"values": ["a"]}, "series": [{"name": "s", "values": [at_bound]}]}
        assert build_chart_spec(spec) is not None
        # One past the bound is rejected.
        over = {"type": "bar", "x": {"values": ["a"]}, "series": [{"name": "s", "values": [at_bound + 1]}]}
        assert build_chart_spec(over) is None

    def test_huge_int_mixed_with_valid_values_is_rejected(self):
        spec = {"type": "line", "x": {"values": ["a", "b"]}, "series": [{"name": "s", "values": [1, 10**500]}]}
        assert build_chart_spec(spec) is None


VALID_BAR = {
    "type": "bar",
    "version": CHART_CONTRACT_VERSION,
    "title": "Top services",
    "summary": "EKS leads.",
    "unit": "USD",
    "x": {"label": "Service", "values": ["Amazon EKS", "Amazon GameLift"]},
    "y": {"label": "USD"},
    "series": [{"name": "UnblendedCost", "values": [80.0, 65.0]}],
}


def _extract_fence_json(fence: str) -> dict:
    assert fence.startswith("```chart\n")
    assert fence.endswith("\n```")
    body = fence[len("```chart\n") : -len("\n```")]
    return json.loads(body)


class TestBuildChartSpec:
    def test_accepts_a_well_formed_spec(self):
        assert build_chart_spec(VALID_BAR) is not None

    def test_rejects_unknown_type(self):
        assert build_chart_spec({**VALID_BAR, "type": "pie"}) is None

    def test_rejects_unknown_top_level_key(self):
        assert build_chart_spec({**VALID_BAR, "evil": 1}) is None

    def test_rejects_unknown_nested_keys(self):
        assert build_chart_spec({**VALID_BAR, "x": {"label": "s", "values": ["a", "b"], "evil": 1}}) is None
        assert build_chart_spec({**VALID_BAR, "series": [{"name": "n", "values": [1.0, 2.0], "evil": 1}]}) is None

    def test_rejects_series_length_mismatch(self):
        assert build_chart_spec({**VALID_BAR, "series": [{"name": "n", "values": [1.0]}]}) is None

    def test_rejects_non_finite_and_out_of_range(self):
        assert build_chart_spec({**VALID_BAR, "series": [{"name": "n", "values": [1.0, float("inf")]}]}) is None
        assert build_chart_spec({**VALID_BAR, "series": [{"name": "n", "values": [1.0, MAX_ABS_VALUE * 10]}]}) is None

    def test_rejects_negative_area_values(self):
        spec = {
            "type": "area",
            "x": {"values": ["w1", "w2"]},
            "series": [{"name": "net", "values": [10.0, -5.0]}],
        }
        assert build_chart_spec(spec) is None

    def test_allows_negative_bar_values(self):
        spec = {
            "type": "bar",
            "x": {"values": ["w1", "w2"]},
            "series": [{"name": "net", "values": [10.0, -5.0]}],
        }
        assert build_chart_spec(spec) is not None

    def test_enforces_point_and_series_and_total_bounds(self):
        too_many_points = {
            "type": "line",
            "x": {"values": [f"p{i}" for i in range(MAX_POINTS + 1)]},
            "series": [{"name": "s", "values": [float(i) for i in range(MAX_POINTS + 1)]}],
        }
        assert build_chart_spec(too_many_points) is None

        too_many_series = {
            "type": "bar",
            "x": {"values": ["a", "b"]},
            "series": [{"name": f"s{i}", "values": [1.0, 2.0]} for i in range(MAX_SERIES + 1)],
        }
        assert build_chart_spec(too_many_series) is None

        points = 300
        aggregate = {
            "type": "line",
            "x": {"values": [f"p{i}" for i in range(points)]},
            "series": [{"name": f"s{i}", "values": [float(j) for j in range(points)]} for i in range(8)],
        }
        assert points * 8 > MAX_TOTAL_VALUES
        assert build_chart_spec(aggregate) is None

    def test_rejects_incompatible_version(self):
        assert build_chart_spec({**VALID_BAR, "version": "2.0"}) is None


class TestRenderChartFence:
    def test_renders_a_parseable_contract_valid_fence(self):
        fence = render_chart_fence(VALID_BAR)
        parsed = _extract_fence_json(fence)
        # Round-trips through the same validator (producer == consumer contract).
        assert build_chart_spec(parsed) is not None
        assert parsed["series"][0]["values"] == [80.0, 65.0]

    def test_returns_empty_string_for_invalid_spec(self):
        assert render_chart_fence({"type": "pie"}) == ""


class TestChartDirective:
    def test_directive_is_versioned_and_describes_the_fence(self):
        assert CHART_DIRECTIVE.version == CHART_CONTRACT_VERSION
        assert "```chart" not in CHART_DIRECTIVE.text  # it references the tag, not a literal fence
        assert "`chart`" in CHART_DIRECTIVE.text
        assert CHART_CONTRACT_VERSION in CHART_DIRECTIVE.text
        assert '"type":"line|bar|area"' in CHART_DIRECTIVE.text

    def test_with_chart_directive_appends_to_a_prompt(self):
        composed = with_chart_directive("BASE PROMPT")
        assert composed.startswith("BASE PROMPT")
        assert CHART_DIRECTIVE.text in composed
