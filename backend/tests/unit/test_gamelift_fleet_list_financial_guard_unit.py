"""The fleet listing must survive the orchestrator's financial guard (#464).

For a non-cost query the orchestrator runs
``financial_guard.contains_unvalidated_financial_content`` over the final
response and withholds the whole section if it looks like it carries an
unvalidated monetary value. A GameLift fleet listing is operational data, not a
cost report, so it must pass that guard: neither the code-owned tool-output
envelope the model receives, nor a prompt-conformant markdown rendering of it,
may be mistaken for financial content and withheld.

These tests build the envelope from this module's own projections
(``project_classic_fleet`` / ``project_container_fleet_summary`` /
``build_fleet_list_result``) for a typical mixed listing (one instance and one
container fleet) and for a degraded listing (a refused ``denied`` listing and a
throttled ``incomplete`` listing), render each the way the orchestrator sees it,
and assert the guard keeps them.

The guard is keyed on currency markers rather than bare numbers, so the test
also proves it is not vacuous: injecting a money-shaped billing figure into the
same rendering trips the guard. The envelope carries the container
``BillingType`` enum (``ON_DEMAND``/``SPOT``) and typed signal codes
(``access_denied``, ``throttled``); it never carries a currency amount or
provider phrasing such as "rate exceeded", which is why a faithful rendering
stays clean.
"""

# Standard library
import json
from typing import Any

# Third-party packages
import pytest

# Local modules
from agents.financial_guard import contains_unvalidated_financial_content
from agents.gamelift_projections import (
    build_fleet_list_result,
    project_classic_fleet,
    project_container_fleet_summary,
)

pytestmark = pytest.mark.unit


def _mixed_listing() -> dict[str, Any]:
    """A typical mixed listing: one classic (instance) fleet and one container fleet."""
    classic = project_classic_fleet(
        {
            "FleetId": "fleet-11111111-2222-3333-4444-5555aaaa6666",
            "Name": "prod game fleet",
            "Status": "ACTIVE",
            "FleetType": "ON_DEMAND",
            "ComputeType": "EC2",
            "InstanceType": "c6i.large",
            "OperatingSystem": "AMAZON_LINUX_2023",
            "NewGameSessionProtectionPolicy": "FullProtection",
        }
    )
    container = project_container_fleet_summary(
        {
            "FleetType": "container",
            "Status": "ACTIVE",
            "InstanceType": "c6i.large",
            "BillingType": "ON_DEMAND",
            "GameServerContainerGroupDefinitionName": "game-server-group",
            "GameServerContainerGroupDefinitionVersion": 4,
            "GameServerContainerGroupsPerInstance": 2,
            "DeploymentStatus": "COMPLETE",
            "LocationCount": 3,
        }
    )
    assert classic and container, "fixture rows must survive projection"
    return build_fleet_list_result(classic_rows=[classic], container_rows=[container], warnings=[])


def _denied_listing() -> dict[str, Any]:
    """A refused listing: both sub-listings denied, no rows retained."""
    return build_fleet_list_result(
        classic_rows=[],
        container_rows=[],
        warnings=[
            {"Source": "list_fleets", "Code": "access_denied"},
            {"Source": "list_container_fleets", "Code": "access_denied"},
        ],
        listing_denied=True,
        error_code="access_denied",
    )


def _incomplete_listing() -> dict[str, Any]:
    """A degraded listing: one fleet retained, the other sub-read throttled."""
    classic = project_classic_fleet(
        {
            "FleetId": "fleet-99999999-8888-7777-6666-5555bbbb4444",
            "Name": "staging fleet",
            "Status": "ACTIVE",
            "FleetType": "SPOT",
            "ComputeType": "EC2",
            "InstanceType": "c6i.large",
            "OperatingSystem": "AMAZON_LINUX_2023",
        }
    )
    assert classic
    return build_fleet_list_result(
        classic_rows=[classic],
        container_rows=[],
        warnings=[{"Source": "list_container_fleets", "Code": "throttled"}],
        error_code="throttled",
    )


def _render_mixed_markdown(result: dict[str, Any]) -> str:
    """Render the mixed listing as the GameLift prompt instructs: headers, bold, tables.

    ``BillingType`` is a column header, not a label glued to a count, so the
    financial-value grammar has no money-shaped number on a billing label line.
    """
    container = result["ContainerFleets"][0]
    classic = result["ClassicFleets"][0]
    counts = result["FleetCounts"]
    return (
        "## GameLift Fleets\n\n"
        f"**Total: {counts['Total']}** (Classic: {counts['Classic']}, Container: {counts['Container']}).\n\n"
        "**Classic fleets**\n\n"
        "| Name | Status | Fleet type | Instance type |\n"
        "|------|--------|------------|---------------|\n"
        f"| {classic['Name']} | {classic['Status']} | {classic['FleetType']} | {classic['InstanceType']} |\n\n"
        "**Container fleets**\n\n"
        "| Group definition | Status | Instance type | Billing | Groups/instance | Locations |\n"
        "|------------------|--------|---------------|---------|-----------------|-----------|\n"
        f"| {container['GameServerContainerGroupDefinitionName']} | {container['Status']} | "
        f"{container['InstanceType']} | {container['BillingType']} | "
        f"{container['GameServerContainerGroupsPerInstance']} | {container['LocationCount']} |\n"
    )


def _render_degraded_markdown(result: dict[str, Any]) -> str:
    """Render a degraded listing, relaying the status and typed codes faithfully."""
    status = result["status"]
    error_code = result.get("error", {}).get("code")
    retained = result["FleetCounts"]["Total"]
    lines = ["## GameLift Fleets\n"]
    if status == "denied":
        lines.append(
            f"I could not list your fleets: the request was refused (code `{error_code}`). "
            "This is a refused listing, not an empty inventory.\n"
        )
    else:
        lines.append(
            f"This is a partial view (status `{status}`"
            + (f", code `{error_code}`" if error_code else "")
            + f"). {retained} fleet(s) were returned; at least one listing did not complete, "
            "so this is not a complete inventory.\n"
        )
    return "\n".join(lines)


class TestFleetListingSurvivesFinancialGuard:
    """A fleet listing is operational data and must not be withheld as financial."""

    def test_mixed_listing_envelope_is_not_financial(self) -> None:
        result = _mixed_listing()
        assert result["status"] == "ok"
        # The envelope is the exact tool output the model receives.
        assert not contains_unvalidated_financial_content(json.dumps(result))

    def test_mixed_listing_markdown_render_is_not_financial(self) -> None:
        rendering = _render_mixed_markdown(_mixed_listing())
        assert "Billing" in rendering  # the container billing column is present
        assert not contains_unvalidated_financial_content(rendering)

    def test_denied_listing_envelope_is_not_financial(self) -> None:
        result = _denied_listing()
        assert result["status"] == "denied"
        assert not contains_unvalidated_financial_content(json.dumps(result))

    def test_denied_listing_markdown_render_is_not_financial(self) -> None:
        rendering = _render_degraded_markdown(_denied_listing())
        assert "access_denied" in rendering
        assert not contains_unvalidated_financial_content(rendering)

    def test_incomplete_listing_envelope_and_render_are_not_financial(self) -> None:
        result = _incomplete_listing()
        assert result["status"] == "incomplete"
        assert not contains_unvalidated_financial_content(json.dumps(result))
        rendering = _render_degraded_markdown(result)
        # The typed code is relayed verbatim; it is not provider phrasing such
        # as "rate exceeded", which would carry a financial-looking token.
        assert "throttled" in rendering
        assert not contains_unvalidated_financial_content(rendering)

    def test_guard_is_not_vacuous_money_shaped_billing_trips_it(self) -> None:
        """A money-shaped billing figure in the same rendering must be withheld.

        This proves the passing assertions above are meaningful: the guard does
        fire when a currency amount appears next to a billing label, so a clean
        pass is a property of the listing, not of an inert guard. It also marks
        the boundary the optional model-visible-field rename would move: a plain
        ``Billing:`` label glued to a bare count on one line already trips the
        financial-value grammar even with no currency amount.
        """
        clean = _render_mixed_markdown(_mixed_listing())
        assert not contains_unvalidated_financial_content(clean)

        # A money-shaped billing line added to the rendering is withheld.
        billing_amount = clean + "\n\nMonthly billing: $999.00 for the container fleet.\n"
        assert contains_unvalidated_financial_content(billing_amount)

        # Even without a currency symbol, a billing label glued to a bare number
        # on a single line trips the value-language grammar. This is why a
        # single-line bullet such as "Billing: ON_DEMAND, 2 groups" is risky and
        # the markdown render keeps billing in a header cell instead.
        billing_count_line = clean + "\n\n- containerfleet (Billing: 2 groups per instance)\n"
        assert contains_unvalidated_financial_content(billing_count_line)
