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
    def test_factory_receives_the_four_runtime_tools(self):
        # Observe the ACTUAL additional_tools argument handed to
        # create_specialist_agent at module construction time via an import-time
        # capture hook, NOT a post-construction alias. Dropping a tool from
        # additional_tools must fail here.
        # Local modules
        import agents.gamelift_specialist as gls

        captured = gls.get_captured_specialist_registration()
        assert captured is not None, "no create_specialist_agent call was captured at import time"
        registered = captured["additional_tools"]
        names = {getattr(t, "__name__", getattr(t, "tool_name", None)) for t in registered}
        for name in ("list_gamelift_fleets", "get_fleet_utilization", "get_fleet_capacity", "get_scaling_policies"):
            assert name in names, f"{name} missing from the tools handed to create_specialist_agent"
        assert len(registered) == 4
        # The captured service name confirms this is the GameLift specialist.
        assert captured["service_name"] == "GameLift"

    def test_dropping_a_tool_from_additional_tools_is_observed(self):
        # Discriminating: rebuild the specialist through the SAME factory path
        # with one tool removed and prove the capture reflects the reduced list.
        # This is what makes the test fail when additional_tools loses a tool.
        # Local modules
        import agents.gamelift_specialist as gls

        baseline = gls.get_captured_specialist_registration()
        full = list(baseline["additional_tools"])
        assert len(full) == 4

        reduced = full[:-1]
        gls.build_gamelift_agent(additional_tools=reduced)
        after = gls.get_captured_specialist_registration()
        assert len(after["additional_tools"]) == 3
        dropped_name = getattr(full[-1], "__name__", getattr(full[-1], "tool_name", None))
        after_names = {getattr(t, "__name__", getattr(t, "tool_name", None)) for t in after["additional_tools"]}
        assert dropped_name not in after_names

        # Restore the canonical registration so module state is not left reduced
        # for other tests importing this module.
        gls.build_gamelift_agent(additional_tools=gls.GAMELIFT_AGENT_TOOLS)
        restored = gls.get_captured_specialist_registration()
        assert len(restored["additional_tools"]) == 4

    def test_runtime_agent_is_built_from_the_captured_tools(self):
        # Prove the tools that were captured are the ones the runtime agent is
        # built from: the module-level gamelift_agent is produced by the same
        # build path that records the capture.
        # Local modules
        import agents.gamelift_specialist as gls

        # Ensure canonical state.
        gls.build_gamelift_agent(additional_tools=gls.GAMELIFT_AGENT_TOOLS)
        captured = gls.get_captured_specialist_registration()
        assert captured["additional_tools"] == gls.GAMELIFT_AGENT_TOOLS

    def test_registration_collection_holds_the_four_runtime_tools(self):
        # Model access is controlled by the exact tool collection passed to
        # create_specialist_agent, NOT by __all__.
        # Local modules
        import agents.gamelift_specialist as gls

        registered = gls.GAMELIFT_AGENT_TOOLS
        names = {getattr(t, "__name__", getattr(t, "tool_name", None)) for t in registered}
        for name in ("list_gamelift_fleets", "get_fleet_utilization", "get_fleet_capacity", "get_scaling_policies"):
            assert name in names, f"{name} missing from GAMELIFT_AGENT_TOOLS (runtime registration contract)"
        assert len(registered) == 4

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
