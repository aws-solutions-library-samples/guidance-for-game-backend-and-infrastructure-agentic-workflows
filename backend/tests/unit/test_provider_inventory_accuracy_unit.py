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
