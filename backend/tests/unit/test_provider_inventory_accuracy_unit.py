"""Inventory accuracy tests for docs/PROVIDER_RESPONSE_INVENTORY.md (#457).

The inventory is a security-relevant document: it states which provider-backed
surfaces are bounded and which still return raw data to the model. A stale claim
is worse than none. These tests pin each corrected claim to the code it
describes so the document cannot silently drift from reality:

* the Knowledge Base ``numberOfResults`` is caller/provider-controlled, not a
  code-owned upper bound (``create_kb_retrieve_tool`` forwards it unchanged);
* both the classic and container GameLift list paths are uncapped to the model;
* ``reuse_cost_report`` is a registered deterministic cost surface with
  no-new-query semantics.
"""

# Standard library
import inspect
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_INVENTORY = Path(__file__).resolve().parents[3] / "docs" / "PROVIDER_RESPONSE_INVENTORY.md"


def _inventory_text() -> str:
    return _INVENTORY.read_text(encoding="utf-8")


class TestKnowledgeBaseCountClaim:
    def test_kb_count_is_forwarded_unchanged_no_code_bound(self):
        # Local modules
        from utils import kb_tools

        source = inspect.getsource(kb_tools.create_kb_retrieve_tool)
        # The model-supplied numberOfResults is passed straight into the tool_use
        # input; there is no min()/clamp enforcing a code-owned maximum.
        assert "numberOfResults" in source
        assert "min(" not in source.split("numberOfResults", 1)[1].split("def ", 1)[0]

    def test_inventory_describes_kb_count_as_caller_controlled(self):
        text = _inventory_text()
        assert "caller" in text.lower() and "numberofresults" in text.lower()
        # Must NOT claim the default 3 "bounds count".
        assert "default 3) bounds count" not in text
        assert "bounds count" not in text


class TestGameLiftListBoundsClaim:
    def test_both_fleet_paths_are_uncapped_in_code(self):
        # Local modules
        from agents import gamelift_specialist as gls

        # Neither the classic nor the container aggregation applies an item cap.
        classic_src = inspect.getsource(gls._list_classic_fleet_attributes)
        container_src = inspect.getsource(gls._list_container_fleet_summaries)
        assert "GAMELIFT_MAX_PROJECTED_ITEMS" not in classic_src
        assert "GAMELIFT_MAX_PROJECTED_ITEMS" not in container_src

    def test_inventory_states_both_paths_uncapped(self):
        text = _inventory_text().lower()
        # Both classic AND container listing outputs must be called out as uncapped.
        assert "classic" in text and "container" in text
        assert "neither classic nor container fleet output is item-bounded" in text


class TestReuseCostReportRegistered:
    def test_reuse_cost_report_is_registered_with_get_cost_report(self):
        # Local modules
        from agents import cost_report

        source = inspect.getsource(cost_report)
        # Both deterministic cost tools are returned from the factory.
        assert "reuse_cost_report" in source
        assert "return [get_cost_report, reuse_cost_report]" in source

    def test_inventory_lists_reuse_cost_report_no_new_query(self):
        text = _inventory_text()
        assert "reuse_cost_report" in text
        low = text.lower()
        assert "no new" in low or "never issues a new" in low or "no-new-query" in low


class TestMalformedDispositionInventoryAccuracy:
    """Bind the inventory's malformed/partial-result claims to production behavior.

    The prior inventory assigned two statuses to a skipped non-dict item
    ("truncated" in one column, "incomplete/malformed_response" in another) and
    promised a hard serialized-char cap that a mixed malformed result could
    exceed. These tests compare the documented contract to the exact production
    status/flag a mixed collection produces.
    """

    def test_inventory_does_not_conflate_skipped_non_dict_with_truncated_status(self):
        text = _inventory_text()
        # The contradictory phrasing that lumped skipped non-dict items under the
        # ``truncated`` status must be gone.
        assert "item/char cap or skipped non-dict items → `truncated`" not in text

    def test_inventory_matches_production_status_for_mixed_non_dict_collection(self):
        # Local modules
        from agents.gamelift_projections import project_fleet_utilization

        raw = {
            "FleetUtilization": [
                "malformed",
                {"FleetId": "fleet-1", "ActiveServerProcessCount": 3, "Location": "us-west-2"},
            ]
        }
        result = project_fleet_utilization(raw)
        # Production: status=incomplete, error.code=malformed_response, and a
        # SEPARATE truncated=true boolean marker.
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        assert result["truncated"] is True

        text = _inventory_text()
        # The document must describe the status and the boolean marker distinctly
        # and must state that skipped/discarded rows map to
        # incomplete/malformed_response with a truncated flag — not to a
        # ``truncated`` status.
        assert "malformed_response" in text
        low = text.lower()
        assert "truncated: true" in low or "truncated=true" in low or "`truncated` flag" in low

    def test_inventory_does_not_over_promise_a_hard_serialized_char_guarantee(self):
        # The final malformed envelope is now budgeted, but the inventory must
        # describe the budget as applying to the FINAL envelope (not a naive
        # unconditional guarantee that a reader could read as un-scoped).
        text = _inventory_text().lower()
        assert "final" in text and "serialized" in text


class TestBillingMcpMigrationInventoryAccuracy:
    """Bind the inventory's Billing MCP claims to production behavior (#465).

    The model-visible Billing MCP catalog is exactly three tools and the allowed
    forecast/optimization operations are projected by code we own. These tests
    pin the inventory's claims to the guard and projection modules so the
    document cannot drift from the code.
    """

    def test_only_three_billing_tools_are_model_visible_in_code(self):
        # Local modules
        from agents import cost_mcp_guard as guard

        assert guard._ALLOWED_TOOLS == {"cost-explorer", "compute-optimizer", "cost-optimization"}
        # Blocked historical ops still carry a get_cost_report redirect.
        assert "getCostAndUsage" in guard._CE_BLOCKED_OPS
        assert "getCostAndUsageWithResources" in guard._CE_BLOCKED_OPS
        assert "get_cost_report" in guard._MSG_BLOCKED_HISTORICAL
        # AccountId is excluded from the allowed Cost Optimization Hub group_by.
        assert "AccountId" not in guard._COH_GROUP_BY
        # Reviewed Compute Optimizer ops are EC2 + Auto Scaling rightsizing only;
        # EBS and Lambda rightsizing are not model-visible (their dependent read
        # permissions were never granted to the chat runtime).
        assert guard._CO_OPS == {
            "get_ec2_instance_recommendations",
            "get_auto_scaling_group_recommendations",
        }
        assert "get_ebs_volume_recommendations" not in guard._CO_OPS
        assert "get_lambda_function_recommendations" not in guard._CO_OPS

    def test_inventory_states_compute_optimizer_narrowed_and_region_bound(self):
        low = _inventory_text().lower()
        # The doc must reflect the EC2+ASG-only surface and the region binding,
        # not the earlier "four rightsizing operations" claim.
        assert "four rightsizing operations" not in low
        assert "get_ec2_instance_recommendations" in low
        assert "get_auto_scaling_group_recommendations" in low
        assert "deployment region" in low

    def test_projection_module_exposes_bounded_projectors(self):
        # Local modules
        from agents import cost_projections as proj

        for name in (
            "project_forecast",
            "project_compute_optimizer",
            "project_cost_optimization_recommendations",
            "project_cost_optimization_summaries",
            "project_cost_optimization_detail",
        ):
            assert hasattr(proj, name)
        # Finite numeric + aggregate bounds exist.
        assert proj.MAX_ITEMS > 0 and proj.MAX_ABS_NUMBER > 0

    def test_inventory_states_billing_forecast_optimization_migrated(self):
        text = _inventory_text()
        low = text.lower()
        # The Cost section must no longer call the allowed Billing ops a follow-up.
        assert "forecast/optimization operations return raw mcp json" not in low
        assert "migrated" in low
        # The removed tools (historical-spend bypass risk) are called out.
        assert "removed" in low
        assert "no longer model-visible" in low or "dropped from the model-visible catalog" in low

    def test_inventory_removes_billing_from_remaining_followups(self):
        text = _inventory_text().lower()
        # The "Remaining" bullet must no longer list Billing forecast/optimization.
        remaining = text.split("remaining (explicit follow-ups", 1)[-1]
        assert "no longer a follow-up" in remaining


class TestBillingScopedForecastInventoryAccuracy:
    """Pin the corrected Billing MCP inventory claims to the code they describe
    (#465): cost-forecast-only with code-owned scope, Hub truncation now
    flagged, the Compute Optimizer mixed-row wording, the Auto Scaling
    identifier being a customer-chosen group name, and the IAM narrowing being
    tracked separately in #482 rather than 'removed by #482'."""

    def test_cost_explorer_surface_is_cost_forecast_only_in_code(self):
        # Local modules
        from agents import cost_mcp_guard as guard

        assert guard._CE_FORECAST_OPS == {"getCostForecast"}
        assert "getUsageForecast" not in guard._CE_FORECAST_OPS

    def test_not_found_error_code_exists(self):
        # Local modules
        from agents import cost_projections as proj

        assert proj.ERROR_NOT_FOUND == "not_found"

    def test_per_granularity_forecast_horizon_in_code(self):
        # Local modules
        from agents import cost_mcp_guard as guard

        # 3 months DAILY, 18 months MONTHLY (public Cost Explorer API limits).
        assert guard._MAX_FORECAST_DAYS_DAILY <= 100
        assert 540 <= guard._MAX_FORECAST_DAYS_MONTHLY <= 560
        assert guard._MAX_FORECAST_DAYS_MONTHLY > guard._MAX_FORECAST_DAYS_DAILY

    def test_inventory_states_cost_forecast_only_with_scope(self):
        low = _inventory_text().lower()
        assert "getusageforecast" in low  # it is explicitly called out as not model-visible
        assert "only `getcostforecast` is model-visible" in low
        assert "services" in low and "regions" in low
        assert "canonical `dimensions`" in low

    def test_inventory_states_hub_truncation_flagged(self):
        low = _inventory_text().lower()
        assert "bound + 1" in low
        assert "never a silent `ok`" in low

    def test_inventory_states_asg_identifier_is_group_name(self):
        low = _inventory_text().lower()
        assert "customer-chosen group name" in low

    def test_inventory_tracks_iam_narrowing_separately_not_removed_by_482(self):
        low = _inventory_text().lower()
        assert "iam grant removed by #482" not in low
        assert "tracked separately in #482" in low


class TestBillingErrorVocabularyAndProbeRowInventory:
    """Pin the Cost error-code vocabulary and probe-row disposition in the
    inventory (#465): the Cost error-code list includes the typed codes
    data_unavailable and credentials_unavailable, and the Compute Optimizer
    disposition sentence no longer claims a hard-item-cap `truncated` state that
    the over-fetch guard path cannot produce from a single probe row."""

    def test_new_typed_error_codes_exist_in_code(self):
        # Local modules
        from agents import cost_projections as proj

        assert proj.ERROR_DATA_UNAVAILABLE == "data_unavailable"
        assert proj.ERROR_CREDENTIALS_UNAVAILABLE == "credentials_unavailable"

    def test_inventory_lists_new_typed_error_codes(self):
        low = _inventory_text().lower()
        assert "data_unavailable" in low
        assert "credentials_unavailable" in low

    def test_inventory_probe_row_sentence_corrected(self):
        low = _inventory_text().lower()
        # The old wording claimed a wholly-valid payload bounded only by the hard
        # item cap yields `truncated`; through the over-fetch guard a single probe
        # row cannot do that, so the misleading sentence must be gone.
        assert "a wholly valid payload bounded only by the hard item cap" not in low
