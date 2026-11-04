"""The fleet listing must survive the orchestrator's financial guard (#464).

For a non-cost query the orchestrator runs
``financial_guard.contains_unvalidated_financial_content`` over the final
response and withholds the whole section if it looks like it carries an
unvalidated monetary value. A GameLift fleet listing is operational data, not a
cost report, so it must pass that guard: neither the code-owned tool-output
envelope the model receives, nor a realistic markdown rendering of it, may be
mistaken for financial content and withheld.

These tests build the envelope the way the model actually receives it: each
listing is produced by driving the real ``agents.gamelift_specialist`` tool
``list_gamelift_fleets`` with a fake GameLift client, so the mixed, denied, and
throttled-partial envelopes are exactly what the tool returns. A throttled
partial listing therefore relays its typed code through ``Warnings[].Code`` and
carries no top-level ``error`` key, matching the tool.

The listing is rendered two ways, both drawn from the deployed envelope: a table
(the layout pinned for the deployed listing in ``test_financial_guard_unit.py``)
and a bullet layout in the GameLift prompt's own style (``## headers``,
``**bold**``, bullet points). The guard must keep both.

Bare numbers do not trip the guard, but a financial label next to a number does,
so the tests also prove it is not vacuous: a dollar amount on a billing line
trips it, and so does ``Billing: 999``. The envelope carries the container
``BillingType`` enum (``ON_DEMAND``/``SPOT``) and typed signal codes
(``access_denied``, ``throttled``) sourced from ``agents.gamelift_projections``;
it never carries a currency amount or provider phrasing such as "rate exceeded",
which is why a faithful rendering stays clean.
"""

# Standard library
import json
from typing import Any, cast
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from agents.financial_guard import contains_unvalidated_financial_content

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake GameLift client (shape-compatible with test_gamelift_fleet_list_unit.py)
# ---------------------------------------------------------------------------
def _paginator(pages: list[dict[str, Any]] | None = None, error: Exception | None = None) -> MagicMock:
    pag = MagicMock()
    if error is not None:
        pag.paginate.side_effect = error
    else:
        pag.paginate.return_value = pages or []
    return pag


class _FakeGameLift:
    """Minimal fake GameLift client that serves canned pages and describes."""

    def __init__(
        self,
        *,
        list_fleets_pages: list[dict[str, Any]] | None = None,
        list_fleets_error: Exception | None = None,
        container_fleets_pages: list[dict[str, Any]] | None = None,
        container_fleets_error: Exception | None = None,
        group_def_pages: list[dict[str, Any]] | None = None,
        deployments_pages: list[dict[str, Any]] | None = None,
        describe_fleet_attributes: Any = None,
        describe_container_fleet: Any = None,
        describe_container_group_definition: Any = None,
    ) -> None:
        self.invoked_operations: list[str] = []
        self._list_fleets_pages = list_fleets_pages if list_fleets_pages is not None else [{"FleetIds": []}]
        self._list_fleets_error = list_fleets_error
        self._container_fleets_pages = (
            container_fleets_pages if container_fleets_pages is not None else [{"ContainerFleets": []}]
        )
        self._container_fleets_error = container_fleets_error
        self._group_def_pages = group_def_pages if group_def_pages is not None else [{"ContainerGroupDefinitions": []}]
        self._deployments_pages = deployments_pages if deployments_pages is not None else [{"FleetDeployments": []}]
        self._describe_fleet_attributes = describe_fleet_attributes
        self._describe_container_fleet = describe_container_fleet
        self._describe_container_group_definition = describe_container_group_definition

    def get_paginator(self, operation_name: str) -> MagicMock:
        self.invoked_operations.append(operation_name)
        if operation_name == "list_fleets":
            return _paginator(self._list_fleets_pages, self._list_fleets_error)
        if operation_name == "list_container_fleets":
            return _paginator(self._container_fleets_pages, self._container_fleets_error)
        if operation_name == "list_container_group_definitions":
            return _paginator(self._group_def_pages)
        if operation_name == "list_fleet_deployments":
            return _paginator(self._deployments_pages)
        raise AssertionError(f"Unexpected paginator operation: {operation_name}")

    def describe_fleet_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked_operations.append("describe_fleet_attributes")
        if callable(self._describe_fleet_attributes):
            return cast("dict[str, Any]", self._describe_fleet_attributes(**kwargs))
        return cast("dict[str, Any]", self._describe_fleet_attributes or {"FleetAttributes": []})

    def describe_container_fleet(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked_operations.append("describe_container_fleet")
        if callable(self._describe_container_fleet):
            return cast("dict[str, Any]", self._describe_container_fleet(**kwargs))
        return cast("dict[str, Any]", self._describe_container_fleet or {"ContainerFleet": {}})

    def describe_container_group_definition(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked_operations.append("describe_container_group_definition")
        if callable(self._describe_container_group_definition):
            return cast("dict[str, Any]", self._describe_container_group_definition(**kwargs))
        return cast("dict[str, Any]", self._describe_container_group_definition or {"ContainerGroupDefinition": {}})


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "provider text that must never reach the model"}}, operation)


# ---------------------------------------------------------------------------
# Listings built through the real tool (so the envelope is exactly tool output)
# ---------------------------------------------------------------------------
_CLASSIC_RAW = {
    "FleetAttributes": [
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
    ]
}

_CONTAINER_FLEET = {
    "FleetId": "containerfleet-aaaa1111-bbbb-2222-cccc-3333dddd4444",
    "GameServerContainerGroupDefinitionName": "game-server-group",
    "InstanceType": "c6i.large",
    "BillingType": "ON_DEMAND",
    "Status": "ACTIVE",
    "GameServerContainerGroupsPerInstance": 2,
    "MaximumGameServerContainerGroupsPerInstance": 4,
    "DeploymentDetails": {"LatestDeploymentId": "deployment-one"},
    "PlayerGatewayMode": "ENABLED",
    "LocationAttributes": [{"Location": "us-west-2"}, {"Location": "us-east-1"}, {"Location": "eu-west-1"}],
}

_GROUP_DEF = {
    "ContainerGroupDefinition": {
        "Name": "game-server-group",
        "VersionNumber": 4,
        "ContainerGroupType": "GAME_SERVER",
        "Status": "READY",
        "OperatingSystem": "AMAZON_LINUX_2023",
        "TotalMemoryLimitMebibytes": 1024,
        "TotalVcpuLimit": 0.5,
    }
}


def _run_tool(fake: _FakeGameLift) -> dict[str, Any]:
    # Local modules
    from agents.gamelift_specialist import list_gamelift_fleets

    with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
        return cast("dict[str, Any]", list_gamelift_fleets())


def _mixed_listing() -> dict[str, Any]:
    """A typical mixed listing: one classic (instance) fleet and one container fleet."""
    fake = _FakeGameLift(
        list_fleets_pages=[{"FleetIds": ["fleet-11111111-2222-3333-4444-5555aaaa6666"]}],
        container_fleets_pages=[{"ContainerFleets": [{"FleetId": _CONTAINER_FLEET["FleetId"]}]}],
        group_def_pages=[{"ContainerGroupDefinitions": [_GROUP_DEF["ContainerGroupDefinition"]]}],
        deployments_pages=[{"FleetDeployments": [{"DeploymentId": "deployment-one", "DeploymentStatus": "COMPLETE"}]}],
        describe_fleet_attributes=lambda **kw: _CLASSIC_RAW,
        describe_container_fleet=lambda **kw: {"ContainerFleet": _CONTAINER_FLEET},
        describe_container_group_definition=lambda **kw: _GROUP_DEF,
    )
    return _run_tool(fake)


def _denied_listing() -> dict[str, Any]:
    """A refused listing: both sub-listings denied, no rows retained."""
    fake = _FakeGameLift(
        list_fleets_error=_client_error("AccessDeniedException", "ListFleets"),
        container_fleets_error=_client_error("AccessDeniedException", "ListContainerFleets"),
    )
    return _run_tool(fake)


def _incomplete_listing() -> dict[str, Any]:
    """A degraded listing: one classic fleet retained, the container sub-read throttled."""
    fake = _FakeGameLift(
        list_fleets_pages=[{"FleetIds": ["fleet-99999999-8888-7777-6666-5555bbbb4444"]}],
        container_fleets_error=_client_error("ThrottlingException", "ListContainerFleets"),
        describe_fleet_attributes=lambda **kw: {
            "FleetAttributes": [
                {
                    "FleetId": "fleet-99999999-8888-7777-6666-5555bbbb4444",
                    "Name": "staging fleet",
                    "Status": "ACTIVE",
                    "FleetType": "SPOT",
                    "ComputeType": "EC2",
                    "InstanceType": "c6i.large",
                    "OperatingSystem": "AMAZON_LINUX_2023",
                }
            ]
        },
    )
    return _run_tool(fake)


# ---------------------------------------------------------------------------
# Renderings drawn from the deployed envelope
# ---------------------------------------------------------------------------
def _render_mixed_table(result: dict[str, Any]) -> str:
    """Render the mixed listing as a table, matching the deployed listing layout.

    This mirrors the table pinned for the deployed listing in
    ``test_financial_guard_unit.py``: billing is a column header with the enum
    value in its own cell, never a label glued to a count on one line.
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


def _render_mixed_bullets(result: dict[str, Any]) -> str:
    """Render the mixed listing in the GameLift prompt's style: headers, bold, bullets.

    The prompt asks for ``## headers``, ``**bold**``, and bullet points, not
    tables. Keeping each billing enum on its own ``**Billing:**`` bullet, rather
    than gluing it to a count on one line, keeps the value-language grammar from
    binding the ``Billing`` label to the groups-per-instance count.
    """
    container = result["ContainerFleets"][0]
    classic = result["ClassicFleets"][0]
    counts = result["FleetCounts"]
    return (
        "## GameLift Fleets\n\n"
        f"**Total:** {counts['Total']} (Classic: {counts['Classic']}, Container: {counts['Container']})\n\n"
        "### Classic fleets\n\n"
        f"- **{classic['Name']}**\n"
        f"  - **Status:** {classic['Status']}\n"
        f"  - **Fleet type:** {classic['FleetType']}\n"
        f"  - **Instance type:** {classic['InstanceType']}\n\n"
        "### Container fleets\n\n"
        f"- **{container['GameServerContainerGroupDefinitionName']}**\n"
        f"  - **Status:** {container['Status']}\n"
        f"  - **Instance type:** {container['InstanceType']}\n"
        f"  - **Billing:** {container['BillingType']}\n"
        f"  - **Groups per instance:** {container['GameServerContainerGroupsPerInstance']}\n"
        f"  - **Locations:** {container['LocationCount']}\n"
    )


def _render_degraded_markdown(result: dict[str, Any]) -> str:
    """Render a degraded listing, relaying the status and the typed code faithfully.

    A throttled partial listing carries no top-level ``error`` key; its typed
    code lives in ``Warnings[].Code``. The relay uses ``error.code`` when present
    and otherwise the first ``Warnings[].Code``.
    """
    status = result["status"]
    error_code = result.get("error", {}).get("code")
    warning_codes = [w.get("Code") for w in result.get("Warnings", []) if w.get("Code")]
    retained = result["FleetCounts"]["Total"]
    lines = ["## GameLift Fleets\n"]
    if status == "denied":
        lines.append(
            f"I could not list your fleets: the request was refused (code `{error_code}`). "
            "This is a refused listing, not an empty inventory.\n"
        )
    else:
        relay = error_code or (warning_codes[0] if warning_codes else None)
        lines.append(
            f"This is a partial view (status `{status}`"
            + (f", code `{relay}`" if relay else "")
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

    def test_mixed_listing_table_render_is_not_financial(self) -> None:
        rendering = _render_mixed_table(_mixed_listing())
        assert "Billing" in rendering  # the container billing column is present
        assert not contains_unvalidated_financial_content(rendering)

    def test_mixed_listing_bullet_render_is_not_financial(self) -> None:
        rendering = _render_mixed_bullets(_mixed_listing())
        assert "**Billing:**" in rendering  # billing on its own bullet, no glued count
        assert not contains_unvalidated_financial_content(rendering)

    def test_denied_listing_envelope_is_not_financial(self) -> None:
        result = _denied_listing()
        assert result["status"] == "denied"
        assert result["error"]["code"] == "access_denied"
        assert not contains_unvalidated_financial_content(json.dumps(result))

    def test_denied_listing_markdown_render_is_not_financial(self) -> None:
        rendering = _render_degraded_markdown(_denied_listing())
        assert "access_denied" in rendering
        assert not contains_unvalidated_financial_content(rendering)

    def test_incomplete_listing_envelope_and_render_are_not_financial(self) -> None:
        result = _incomplete_listing()
        assert result["status"] == "incomplete"
        # The throttled code is carried in Warnings, not a top-level error key.
        assert "error" not in result
        assert any(w.get("Code") == "throttled" for w in result["Warnings"])
        assert not contains_unvalidated_financial_content(json.dumps(result))
        rendering = _render_degraded_markdown(result)
        # The typed code is relayed from Warnings verbatim; it is not provider
        # phrasing such as "rate exceeded", which would carry a money-looking token.
        assert "throttled" in rendering
        assert not contains_unvalidated_financial_content(rendering)

    def test_guard_is_not_vacuous_money_and_value_language_trip_it(self) -> None:
        """A money amount and a billing-label value both trip the guard.

        This proves the passing assertions above are meaningful: the guard does
        fire for a currency amount next to a billing label, and for a billing
        label glued to a bare number on one line, so a clean pass is a property
        of the rendering, not of an inert guard. The single-line form is why the
        renderings keep billing in a table column or on its own bullet.
        """
        clean = _render_mixed_bullets(_mixed_listing())
        assert not contains_unvalidated_financial_content(clean)

        # A money-shaped billing line added to the rendering is withheld.
        billing_amount = clean + "\n\nMonthly billing: $999.00 for the container fleet.\n"
        assert contains_unvalidated_financial_content(billing_amount)

        # Even without a currency symbol, a billing label glued to a bare number
        # on a single line trips the value-language grammar. "Billing: 999" is a
        # money-shaped value, so it is correctly withheld.
        billing_value_line = clean + "\n\n- containerfleet (Billing: 999)\n"
        assert contains_unvalidated_financial_content(billing_value_line)
