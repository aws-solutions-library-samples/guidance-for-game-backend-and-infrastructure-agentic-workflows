"""GameLift chart/routing compatibility over the *projected* outputs (#457).

The projection work (bounding, typed value validation, sanitized errors) must
not silently break the two downstream contracts the GameLift specialist
participates in:

1. **Routing / registration.** The orchestrator routes to ``gamelift_agent`` and
   the specialist exposes exactly four tools whose names and callables the
   registration depends on. Renaming a tool, dropping one, or changing the
   projected operational field names would break routing and the model's ability
   to chart the result.

2. **Charts.** A quantitative GameLift answer (utilization/capacity/scaling
   counts across locations/fleets) is charted through the shared, fail-closed
   chart contract (``build_chart_spec``). These tests build a chart directly from
   the *projected* tool output and prove it is accepted, and — discriminatingly —
   that a chart built from the *raw* pre-projection values (NaN/Inf/oversized)
   would be REJECTED. That is what makes the projection load-bearing for charts,
   not incidental.
"""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from agents.chart_directive import build_chart_spec
from agents.gamelift_projections import project_fleet_utilization

pytestmark = pytest.mark.unit

FLEET_A = "fleet-aaaa-0001"
FLEET_B = "fleet-bbbb-0002"


# ---------------------------------------------------------------------------
# Routing / registration compatibility
# ---------------------------------------------------------------------------
class TestRoutingRegistrationCompatibility:
    def test_specialist_exports_the_four_tool_names(self):
        # Local modules
        import agents.gamelift_specialist as gls

        for name in ("list_gamelift_fleets", "get_fleet_utilization", "get_fleet_capacity", "get_scaling_policies"):
            assert name in gls.__all__, f"{name} missing from __all__ (routing/registration contract)"
            assert callable(getattr(gls, name)), f"{name} is not a callable tool"

    def test_projected_field_names_the_router_and_charts_depend_on_are_stable(self):
        # These operational field names are the axis/series source for charts and
        # the keys the model reasons over when routing follow-ups. Pin them.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {
                    "FleetId": FLEET_A,
                    "ActiveServerProcessCount": 4,
                    "ActiveGameSessionCount": 2,
                    "CurrentPlayerSessionCount": 7,
                    "MaximumPlayerSessionCount": 50,
                    "Location": "us-west-2",
                }
            ]
        }
        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_utilization.return_value = raw
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_A)
        row = result["FleetUtilization"][0]
        for field in (
            "FleetId",
            "ActiveServerProcessCount",
            "ActiveGameSessionCount",
            "CurrentPlayerSessionCount",
            "MaximumPlayerSessionCount",
            "Location",
        ):
            assert field in row, f"projected field {field} disappeared — chart/routing regression"


# ---------------------------------------------------------------------------
# Chart compatibility built from projected outputs
# ---------------------------------------------------------------------------
class TestChartCompatibilityOverProjectedOutputs:
    def _projected_multi_location(self):
        raw = {
            "FleetUtilization": [
                {"FleetId": FLEET_A, "CurrentPlayerSessionCount": 7, "Location": "us-west-2"},
                {"FleetId": FLEET_A, "CurrentPlayerSessionCount": 12, "Location": "eu-west-1"},
                {"FleetId": FLEET_A, "CurrentPlayerSessionCount": 3, "Location": "ap-south-1"},
            ]
        }
        return project_fleet_utilization(raw)

    def test_bar_chart_from_projected_utilization_is_contract_valid(self):
        projected = self._projected_multi_location()
        rows = projected["FleetUtilization"]
        spec = {
            "type": "bar",
            "title": "Current player sessions by location",
            "summary": "eu-west-1 leads.",
            "x": {"label": "Location", "values": [r["Location"] for r in rows]},
            "y": {"label": "Sessions"},
            "series": [{"name": "CurrentPlayerSessionCount", "values": [r["CurrentPlayerSessionCount"] for r in rows]}],
        }
        assert build_chart_spec(spec) is not None

    def test_chart_from_projected_output_never_carries_infinity(self):
        # Raw provider values include a non-finite count; after projection that
        # value is dropped, so a chart built from the projected output cannot
        # inherit the NaN/Inf that would make the chart contract reject it.
        raw = {
            "FleetUtilization": [
                {"FleetId": FLEET_A, "CurrentPlayerSessionCount": 7, "Location": "us-west-2"},
                {"FleetId": FLEET_B, "CurrentPlayerSessionCount": float("inf"), "Location": "eu-west-1"},
            ]
        }
        projected = project_fleet_utilization(raw)
        rows = projected["FleetUtilization"]
        # The Inf-bearing row lost its count (dropped by value validation).
        assert all("CurrentPlayerSessionCount" not in r or r["CurrentPlayerSessionCount"] != float("inf") for r in rows)

        # Discriminating: a chart built from the RAW values is rejected...
        raw_spec = {
            "type": "bar",
            "x": {"values": ["us-west-2", "eu-west-1"]},
            "series": [{"name": "sessions", "values": [7, float("inf")]}],
        }
        assert build_chart_spec(raw_spec) is None

        # ...while a chart built only from projected, finite counts is accepted.
        finite = [(r["Location"], r["CurrentPlayerSessionCount"]) for r in rows if "CurrentPlayerSessionCount" in r]
        projected_spec = {
            "type": "bar",
            "x": {"values": [loc for loc, _ in finite]},
            "series": [{"name": "sessions", "values": [v for _, v in finite]}],
        }
        assert build_chart_spec(projected_spec) is not None
