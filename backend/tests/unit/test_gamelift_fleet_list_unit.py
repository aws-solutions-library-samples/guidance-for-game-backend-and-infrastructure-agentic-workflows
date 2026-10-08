"""Unit tests for ``list_gamelift_fleets`` bounded, code-owned projections (#464).

``list_gamelift_fleets`` returns a bounded, code-owned envelope. These tests pin
that it:

* never exposes raw ``describe_fleet_attributes`` classic items (``FleetArn``
  with the account ID, ``InstanceRoleArn``, ``LogPaths``, launch paths/
  parameters, ``MetricGroups``, free-text ``Description``, arbitrary future
  fields), and emits no raw ``FleetAttributes`` duplicate alongside
  ``ClassicFleets``;
* value-validates and bounds the container summaries, not just field-allowlists
  them;
* bounds every page with an item cap and an aggregate serialized-size cap, and
  bounds the per-fleet describe / deployment calls;
* keeps raw provider ``str(e)`` text out of ``Warnings`` and ``error``;
* carries a distinct ``status``/flags vocabulary (ok/empty/denied/incomplete/
  truncated).

These tests attack ALLOWED fields with synthetic hostile values, drive
unbounded pagination and large accounts, feed raw provider error text, capture
the real Loguru sink, assert the exact final-envelope serialized cap, prove each
distinct state, and record the exact boto3 operations invoked (which must be a
subset of the reviewed GameLift operation set).
"""

# Standard library
import json
from typing import Any
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

# Synthetic sensitive values. None may appear in the model-visible envelope.
SYNTHETIC_ACCOUNT_ID = "123456789012"
SYNTHETIC_FLEET_ARN = f"arn:aws:gamelift:us-west-2:{SYNTHETIC_ACCOUNT_ID}:fleet/fleet-classic-0001"
SYNTHETIC_ROLE_ARN = f"arn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/GameLiftFleetRole"
SYNTHETIC_URL = "https://internal.example.invalid/secret?token=abc123"
SYNTHETIC_IP = "10.11.12.13"


def _assert_no_sensitive(payload: dict[str, Any]) -> None:
    blob = json.dumps(payload, default=str)
    assert SYNTHETIC_ACCOUNT_ID not in blob, f"account id leaked: {blob}"
    assert "arn:aws" not in blob, f"ARN leaked: {blob}"
    assert SYNTHETIC_URL not in blob, f"URL leaked: {blob}"
    assert "example.invalid" not in blob, f"network coordinate leaked: {blob}"
    assert SYNTHETIC_IP not in blob, f"IP leaked: {blob}"
    assert "/local/game/" not in blob, f"log path leaked: {blob}"


def _paginator(pages: list[dict[str, Any]] | None = None, error: Exception | None = None) -> MagicMock:
    pag = MagicMock()
    if error is not None:
        pag.paginate.side_effect = error
    else:
        pag.paginate.return_value = pages or []
    return pag


class _FakeGameLift:
    """Minimal fake GameLift client that records the operations it is asked for.

    It records every paginator operation name and every direct describe/list
    call so a test can assert the tool invokes only the reviewed operation set.
    """

    def __init__(
        self,
        *,
        list_fleets_pages: list[dict[str, Any]] | None = None,
        container_fleets_pages: list[dict[str, Any]] | None = None,
        group_def_pages: list[dict[str, Any]] | None = None,
        deployments_pages: list[dict[str, Any]] | None = None,
        describe_fleet_attributes: Any = None,
        describe_container_fleet: Any = None,
        describe_container_group_definition: Any = None,
    ) -> None:
        self.invoked_operations: list[str] = []
        self._list_fleets_pages = list_fleets_pages if list_fleets_pages is not None else [{"FleetIds": []}]
        self._container_fleets_pages = (
            container_fleets_pages if container_fleets_pages is not None else [{"ContainerFleets": []}]
        )
        self._group_def_pages = group_def_pages if group_def_pages is not None else [{"ContainerGroupDefinitions": []}]
        self._deployments_pages = deployments_pages if deployments_pages is not None else [{"FleetDeployments": []}]
        self._describe_fleet_attributes = describe_fleet_attributes
        self._describe_container_fleet = describe_container_fleet
        self._describe_container_group_definition = describe_container_group_definition

    # Paginator interface -------------------------------------------------
    def get_paginator(self, operation_name: str) -> MagicMock:
        self.invoked_operations.append(operation_name)
        if operation_name == "list_fleets":
            return _paginator(self._list_fleets_pages)
        if operation_name == "list_container_fleets":
            return _paginator(self._container_fleets_pages)
        if operation_name == "list_container_group_definitions":
            return _paginator(self._group_def_pages)
        if operation_name == "list_fleet_deployments":
            return _paginator(self._deployments_pages)
        raise AssertionError(f"Unexpected paginator operation: {operation_name}")

    # Direct call interface ----------------------------------------------
    def describe_fleet_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked_operations.append("describe_fleet_attributes")
        if callable(self._describe_fleet_attributes):
            return self._describe_fleet_attributes(**kwargs)
        return self._describe_fleet_attributes or {"FleetAttributes": []}

    def describe_container_fleet(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked_operations.append("describe_container_fleet")
        if callable(self._describe_container_fleet):
            return self._describe_container_fleet(**kwargs)
        return self._describe_container_fleet or {"ContainerFleet": {}}

    def describe_container_group_definition(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked_operations.append("describe_container_group_definition")
        if callable(self._describe_container_group_definition):
            return self._describe_container_group_definition(**kwargs)
        return self._describe_container_group_definition or {"ContainerGroupDefinition": {}}


# The reviewed read-only GameLift operation set. The tool must invoke only a
# subset of this set (no mutating or unreviewed operations).
ALLOWED_GAMELIFT_OPERATIONS = {
    "list_fleets",
    "describe_fleet_attributes",
    "list_container_fleets",
    "list_container_group_definitions",
    "describe_container_fleet",
    "describe_container_group_definition",
    "list_fleet_deployments",
    "describe_fleet_utilization",
    "describe_fleet_capacity",
    "describe_scaling_policies",
}


# ---------------------------------------------------------------------------
# Envelope shape and vocabulary
# ---------------------------------------------------------------------------
class TestListEnvelopeVocabulary:
    def test_empty_account_is_status_empty_no_raw_fleet_attributes(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        fake = _FakeGameLift()
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert result["status"] == "empty"
        assert result["ClassicFleets"] == []
        assert result["ContainerFleets"] == []
        # The raw duplicate collection must be gone from the envelope.
        assert "FleetAttributes" not in result
        assert result["FleetCounts"] == {"Classic": 0, "Container": 0, "Total": 0}
        # No describe_fleet_attributes when there are no classic fleet IDs.
        assert "describe_fleet_attributes" not in fake.invoked_operations

    def test_warnings_are_code_owned_source_code_pairs_no_provider_text(self):
        # Container listing fails with raw provider text; the warning that
        # reaches the model must be a code-owned {Source, Code} pair, never the
        # provider message.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ["fleet-classic-0001"]}],
            container_fleets_pages=None,
            describe_fleet_attributes=lambda **kw: {
                "FleetAttributes": [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]
            },
        )
        # Make the container paginator raise provider text.
        fake._container_fleets_pages = None

        def get_paginator(operation_name: str):
            fake.invoked_operations.append(operation_name)
            if operation_name == "list_fleets":
                return _paginator([{"FleetIds": ["fleet-classic-0001"]}])
            if operation_name == "list_container_fleets":
                return _paginator(error=Exception(f"denied for {SYNTHETIC_FLEET_ARN} at {SYNTHETIC_URL}"))
            raise AssertionError(operation_name)

        fake.get_paginator = get_paginator  # type: ignore[assignment]

        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        warnings = result["Warnings"]
        assert warnings, "expected a code-owned warning for the failed container listing"
        for warning in warnings:
            assert set(warning.keys()) == {"Source", "Code", "Count"}, f"warning carries extra/provider keys: {warning}"
            assert "Message" not in warning
            assert isinstance(warning["Count"], int) and warning["Count"] >= 1
        _assert_no_sensitive(result)
        # Classic rows still returned; result is partial (a sub-call failed).
        assert result["ClassicFleets"] == [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]
        assert result["status"] == "incomplete"
        assert result.get("partial") is True


# ---------------------------------------------------------------------------
# Classic-fleet projection (the big gap on main)
# ---------------------------------------------------------------------------
class TestClassicFleetProjection:
    def _classic_raw(self) -> dict[str, Any]:
        return {
            "FleetAttributes": [
                {
                    "FleetId": "fleet-classic-0001",
                    "FleetArn": SYNTHETIC_FLEET_ARN,
                    "Name": "prod-fleet",
                    "Status": "ACTIVE",
                    "FleetType": "ON_DEMAND",
                    "ComputeType": "EC2",
                    "InstanceType": "c5.large",
                    "OperatingSystem": "AMAZON_LINUX_2023",
                    "NewGameSessionProtectionPolicy": "FullProtection",
                    "Description": "free text that could contain anything at all",
                    "InstanceRoleArn": SYNTHETIC_ROLE_ARN,
                    "LogPaths": ["/local/game/logs/myserver.log"],
                    "ServerLaunchPath": "/local/game/bin/server",
                    "ServerLaunchParameters": f"--secret {SYNTHETIC_URL}",
                    "MetricGroups": ["default"],
                    "BuildArn": SYNTHETIC_FLEET_ARN,
                    "ScriptId": "script-should-not-appear",
                    "SomeFutureField": {"nested": SYNTHETIC_URL},
                }
            ]
        }

    def test_classic_fleet_projects_allowlist_and_drops_sensitive(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ["fleet-classic-0001"]}],
            describe_fleet_attributes=lambda **kw: self._classic_raw(),
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert result["status"] == "ok"
        rows = result["ClassicFleets"]
        assert len(rows) == 1
        row = rows[0]
        # Reviewed operational fields preserved.
        assert row["FleetId"] == "fleet-classic-0001"
        assert row["Name"] == "prod-fleet"
        assert row["Status"] == "ACTIVE"
        assert row["FleetType"] == "ON_DEMAND"
        assert row["ComputeType"] == "EC2"
        assert row["InstanceType"] == "c5.large"
        assert row["OperatingSystem"] == "AMAZON_LINUX_2023"
        assert row["NewGameSessionProtectionPolicy"] == "FullProtection"
        # Sensitive / unreviewed fields dropped.
        for forbidden in (
            "FleetArn",
            "InstanceRoleArn",
            "LogPaths",
            "ServerLaunchPath",
            "ServerLaunchParameters",
            "MetricGroups",
            "Description",
            "BuildArn",
            "ScriptId",
            "SomeFutureField",
        ):
            assert forbidden not in row, f"{forbidden} leaked into classic projection"
        # The raw duplicate collection is gone.
        assert "FleetAttributes" not in result
        _assert_no_sensitive(result)

    def test_classic_hostile_values_in_allowed_fields_dropped(self):
        # Attack ALLOWED fields with hostile values.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        hostile = {
            "FleetAttributes": [
                {
                    "FleetId": SYNTHETIC_FLEET_ARN,  # ARN in an identifier field
                    "Name": "host-10.11.12.13-prod",  # embedded IP in a name
                    "Status": "arn:aws:evil",  # non-enum junk in a token field
                    "InstanceType": {"nested": "blob"},  # wrong scalar type
                    "OperatingSystem": "us-\x00bad",  # control char
                }
            ]
        }
        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ["x"]}],
            describe_fleet_attributes=lambda **kw: hostile,
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        _assert_no_sensitive(result)
        # Every attacked value is dropped; the row has no usable projected field
        # and is therefore discarded, making the collection malformed/partial.
        assert "\x00" not in json.dumps(result)
        assert result["status"] in {"incomplete"}
        assert result["ClassicFleets"] == []

    def test_classic_fleets_item_capped_and_truncated(self):
        # Many classic fleets: output must be item-capped and flagged truncated.
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_ITEMS
        from agents.gamelift_specialist import list_gamelift_fleets

        n = GAMELIFT_MAX_PROJECTED_ITEMS + 60
        ids = [f"fleet-{i:04d}" for i in range(n)]

        def describe(**kw):
            return {
                "FleetAttributes": [
                    {"FleetId": fid, "Status": "ACTIVE", "InstanceType": "c5.large"} for fid in kw["FleetIds"]
                ]
            }

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ids}],
            describe_fleet_attributes=describe,
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert len(result["ClassicFleets"]) <= GAMELIFT_MAX_PROJECTED_ITEMS
        assert result.get("truncated") is True
        assert result["status"] in {"truncated", "incomplete"}
        # Counts reflect returned rows, never overclaim completeness.
        assert result["FleetCounts"]["Classic"] == len(result["ClassicFleets"])

    def test_describe_calls_are_bounded_for_large_accounts(self):
        # A very large account must not trigger an unbounded number of describe
        # calls: the tool caps how many fleet IDs it describes.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        ids = [f"fleet-{i:05d}" for i in range(5000)]

        def describe(**kw):
            assert len(kw["FleetIds"]) <= 100  # 100-ID chunking preserved
            return {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in kw["FleetIds"]]}

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ids}],
            describe_fleet_attributes=describe,
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        describe_calls = sum(1 for op in fake.invoked_operations if op == "describe_fleet_attributes")
        # Bounded: at most the number of 100-ID chunks needed to fill the item
        # cap (not 50 chunks for 5000 IDs).
        assert describe_calls <= 2, f"unbounded describe fan-out: {describe_calls} calls"


# ---------------------------------------------------------------------------
# Container-fleet projection: value validation + bounds
# ---------------------------------------------------------------------------
class TestContainerFleetProjection:
    def _container_setup(self, fleet_overrides: dict[str, Any] | None = None) -> _FakeGameLift:
        fleet = {
            "FleetId": "container-fleet-1",
            "FleetArn": SYNTHETIC_FLEET_ARN,
            "FleetRoleArn": SYNTHETIC_ROLE_ARN,
            "GameServerContainerGroupDefinitionName": "game-server-group",
            "GameServerContainerGroupDefinitionArn": "container-group-definition/game-server-group:7",
            "InstanceType": "c6i.large",
            "BillingType": "ON_DEMAND",
            "Status": "ACTIVE",
            "GameServerContainerGroupsPerInstance": 2,
            "MaximumGameServerContainerGroupsPerInstance": 4,
            "DeploymentDetails": {"LatestDeploymentId": "deployment-one"},
            "LogConfiguration": {"LogDestination": "CLOUDWATCH", "LogGroupArn": "log-group-arn"},
            "PlayerGatewayMode": "ENABLED",
            "LocationAttributes": [{"Location": "us-west-2"}],
        }
        if fleet_overrides:
            fleet.update(fleet_overrides)
        return _FakeGameLift(
            container_fleets_pages=[{"ContainerFleets": [{"FleetId": "container-fleet-1"}]}],
            group_def_pages=[
                {
                    "ContainerGroupDefinitions": [
                        {
                            "Name": "game-server-group",
                            "VersionNumber": 7,
                            "ContainerGroupType": "GAME_SERVER",
                            "Status": "READY",
                            "OperatingSystem": "AMAZON_LINUX_2023",
                            "TotalMemoryLimitMebibytes": 1024,
                            "TotalVcpuLimit": 0.5,
                        }
                    ]
                }
            ],
            deployments_pages=[
                {"FleetDeployments": [{"DeploymentId": "deployment-one", "DeploymentStatus": "COMPLETE"}]}
            ],
            describe_container_fleet=lambda **kw: {"ContainerFleet": fleet},
            describe_container_group_definition=lambda **kw: {
                "ContainerGroupDefinition": {
                    "Name": "game-server-group",
                    "VersionNumber": 7,
                    "ContainerGroupType": "GAME_SERVER",
                    "Status": "READY",
                    "OperatingSystem": "AMAZON_LINUX_2023",
                    "TotalMemoryLimitMebibytes": 1024,
                    "TotalVcpuLimit": 0.5,
                }
            },
        )

    def test_container_fleet_projection_validates_and_drops_sensitive(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        fake = self._container_setup()
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert result["status"] == "ok"
        row = result["ContainerFleets"][0]
        assert row["FleetType"] == "container"
        assert row["Status"] == "ACTIVE"
        assert row["InstanceType"] == "c6i.large"
        assert row["BillingType"] == "ON_DEMAND"
        assert row["DeploymentStatus"] == "COMPLETE"
        assert row["LogDestinationType"] == "CLOUDWATCH"
        assert row["PlayerGatewayMode"] == "ENABLED"
        assert row["GameServerContainerGroupDefinitionVersion"] == 7
        assert row["ContainerGroupDefinition"]["Status"] == "READY"
        # Sensitive / raw never present.
        assert "FleetArn" not in row
        assert "FleetRoleArn" not in row
        assert "LogGroupArn" not in row
        assert "FleetId" not in row
        _assert_no_sensitive(result)

    def test_container_hostile_enum_and_log_destination_dropped(self):
        # Attack the allowed enum fields with non-enum junk.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        fake = self._container_setup(
            fleet_overrides={
                "Status": "arn:aws:evil",  # junk in enum
                "LogConfiguration": {"LogDestination": SYNTHETIC_URL},  # URL in enum
                "PlayerGatewayMode": "10.11.12.13",  # IP in enum
                "InstanceType": "x" * 500,  # oversized string
            }
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        row = result["ContainerFleets"][0]
        assert "Status" not in row
        assert "LogDestinationType" not in row
        assert "PlayerGatewayMode" not in row
        assert "InstanceType" not in row
        # The constant FleetType marker still survives.
        assert row["FleetType"] == "container"
        _assert_no_sensitive(result)

    def test_container_describe_fan_out_is_bounded(self):
        # Many container fleets must not trigger an unbounded number of
        # describe_container_fleet / list_fleet_deployments calls.
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_ITEMS
        from agents.gamelift_specialist import list_gamelift_fleets

        n = GAMELIFT_MAX_PROJECTED_ITEMS + 80
        listed = [{"FleetId": f"cf-{i:04d}"} for i in range(n)]
        fake = _FakeGameLift(
            container_fleets_pages=[{"ContainerFleets": listed}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": {"FleetId": kw["FleetId"], "Status": "ACTIVE"}},
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        describe_calls = sum(1 for op in fake.invoked_operations if op == "describe_container_fleet")
        deployment_pages = sum(1 for op in fake.invoked_operations if op == "list_fleet_deployments")
        assert describe_calls <= GAMELIFT_MAX_PROJECTED_ITEMS, f"unbounded container describe: {describe_calls}"
        assert deployment_pages <= GAMELIFT_MAX_PROJECTED_ITEMS, f"unbounded deployment reads: {deployment_pages}"
        assert len(result["ContainerFleets"]) <= GAMELIFT_MAX_PROJECTED_ITEMS
        assert result.get("truncated") is True


# ---------------------------------------------------------------------------
# Distinct states
# ---------------------------------------------------------------------------
class TestDistinctStates:
    def test_denied_when_both_listings_refused(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        denied = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": f"nope {SYNTHETIC_FLEET_ARN}"}},
            "ListFleets",
        )

        def get_paginator(operation_name: str):
            if operation_name in ("list_fleets", "list_container_fleets"):
                return _paginator(error=denied)
            return _paginator([])

        fake = _FakeGameLift()
        fake.get_paginator = get_paginator  # type: ignore[assignment]
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert result["status"] == "denied"
        assert result["error"]["code"] == "access_denied"
        assert result["ClassicFleets"] == []
        assert result["ContainerFleets"] == []
        _assert_no_sensitive(result)

    def test_item_cap_marks_truncated(self):
        # More fleet IDs than the item cap can hold means the view is bounded
        # (truncated) even though every returned row is valid. (Real residual
        # list_fleets pagination over multiple pages is covered by the Stubber
        # test in test_gamelift_fleet_list_robustness_unit.py.)
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_ITEMS
        from agents.gamelift_specialist import list_gamelift_fleets

        ids = [f"fleet-{i:04d}" for i in range(GAMELIFT_MAX_PROJECTED_ITEMS + 5)]

        def describe(**kw):
            return {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in kw["FleetIds"]]}

        fake = _FakeGameLift(list_fleets_pages=[{"FleetIds": ids}], describe_fleet_attributes=describe)
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert result.get("truncated") is True
        assert result["status"] in {"truncated", "incomplete"}

    def test_client_construction_failure_is_sanitized_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch(
            "agents.gamelift_specialist.boto3.client",
            side_effect=Exception(f"cannot reach {SYNTHETIC_URL}"),
        ):
            result = list_gamelift_fleets()

        assert result["status"] == "incomplete"
        assert result["ClassicFleets"] == []
        assert result["ContainerFleets"] == []
        _assert_no_sensitive(result)


# ---------------------------------------------------------------------------
# Operation-set and logging guards
# ---------------------------------------------------------------------------
class TestOperationSetAndLogging:
    def test_invoked_operations_are_subset_of_reviewed_set(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ["fleet-classic-0001"]}],
            container_fleets_pages=[{"ContainerFleets": [{"FleetId": "cf-1"}]}],
            describe_fleet_attributes=lambda **kw: {
                "FleetAttributes": [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]
            },
            describe_container_fleet=lambda **kw: {"ContainerFleet": {"FleetId": "cf-1", "Status": "ACTIVE"}},
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            list_gamelift_fleets()

        invoked = set(fake.invoked_operations)
        assert invoked, "no operations were recorded"
        assert (
            invoked <= ALLOWED_GAMELIFT_OPERATIONS
        ), f"tool called operations outside the reviewed set: {invoked - ALLOWED_GAMELIFT_OPERATIONS}"

    def test_failure_log_omits_provider_text_and_fleet_ids(self):
        # The sanitized failure path runs the real Loguru logger; capture the
        # sink and prove no provider message, ARN, account id, or fleet id lands.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets
        from utils.logger import logger

        captured: list[str] = []

        def sink(message):
            captured.append(str(message))
            captured.append(json.dumps(message.record["extra"], default=str))

        sink_id = logger.add(sink, level="DEBUG")

        def get_paginator(operation_name: str):
            if operation_name == "list_fleets":
                return _paginator(error=Exception(f"boom {SYNTHETIC_FLEET_ARN} at {SYNTHETIC_URL}"))
            return _paginator([{"ContainerFleets": []}])

        fake = _FakeGameLift()
        fake.get_paginator = get_paginator  # type: ignore[assignment]
        try:
            with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
                list_gamelift_fleets()
        finally:
            logger.remove(sink_id)

        blob = "\n".join(captured)
        assert SYNTHETIC_FLEET_ARN not in blob
        assert "arn:aws" not in blob
        assert SYNTHETIC_ACCOUNT_ID not in blob
        assert SYNTHETIC_URL not in blob
        assert "boom" not in blob
        # The caller fleet id embedded in the ARN is never logged either.
        assert "fleet-classic-0001" not in blob


# ---------------------------------------------------------------------------
# Aggregate serialized-envelope cap
# ---------------------------------------------------------------------------
class TestAggregateEnvelopeCap:
    def test_final_envelope_within_char_budget(self):
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_CHARS
        from agents.gamelift_specialist import list_gamelift_fleets

        # Many classic fleets, each with legal but non-trivial field values, so
        # the combined serialized envelope would exceed the char budget without
        # aggregate truncation.
        long_name = "fleet-name-" + "a" * 200
        ids = [f"fleet-{i:04d}" for i in range(90)]

        def describe(**kw):
            return {
                "FleetAttributes": [
                    {"FleetId": fid, "Name": long_name, "Status": "ACTIVE", "InstanceType": "c5.large"}
                    for fid in kw["FleetIds"]
                ]
            }

        fake = _FakeGameLift(list_fleets_pages=[{"FleetIds": ids}], describe_fleet_attributes=describe)
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS
        assert result.get("truncated") is True
        assert result["FleetCounts"]["Classic"] == len(result["ClassicFleets"])


# ---------------------------------------------------------------------------
# Mixed-row discards made visible, required FleetId, container marker
# does not mask all-invalid rows, and bounded/deduplicated warnings.
# ---------------------------------------------------------------------------
class TestMixedRowsAreVisiblyPartial:
    def test_classic_mixed_valid_and_discarded_rows_is_incomplete_partial(self):
        # Two described classic fleets: one fully valid, one whose every value is
        # hostile so it projects to nothing. The valid row must be retained, but
        # the discarded row must leave a trace: incomplete + malformed_response +
        # truncated + partial, never a silent ``ok``.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        def describe(**kw):
            return {
                "FleetAttributes": [
                    {"FleetId": "fleet-good-0001", "Status": "ACTIVE", "InstanceType": "c5.large"},
                    {
                        "FleetId": SYNTHETIC_FLEET_ARN,  # ARN in identifier field
                        "Status": "arn:aws:evil",
                        "InstanceType": {"nested": "blob"},
                    },
                ]
            }

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ["fleet-good-0001", "fleet-bad-0002"]}],
            describe_fleet_attributes=describe,
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        # Valid row retained.
        assert result["ClassicFleets"] == [
            {"FleetId": "fleet-good-0001", "Status": "ACTIVE", "InstanceType": "c5.large"}
        ]
        # Discarded row surfaced, not silently ok.
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        assert result.get("truncated") is True
        assert result.get("partial") is True
        assert result["FleetCounts"]["Classic"] == 1
        _assert_no_sensitive(result)

    def test_container_mixed_valid_and_discarded_rows_is_incomplete_partial(self):
        # Two container fleets: one valid, one whose every provider field is
        # hostile. The all-invalid one must be discarded (not survive on the
        # code-owned FleetType marker alone), leaving the listing partial.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        good = {"FleetId": "cf-good", "Status": "ACTIVE", "InstanceType": "c6i.large", "BillingType": "ON_DEMAND"}
        bad = {
            "FleetId": "cf-bad",
            "Status": "arn:aws:evil",
            "InstanceType": {"nested": "blob"},
            "BillingType": SYNTHETIC_URL,
            "PlayerGatewayMode": SYNTHETIC_IP,
        }
        fleets_by_id = {"cf-good": good, "cf-bad": bad}

        fake = _FakeGameLift(
            container_fleets_pages=[{"ContainerFleets": [{"FleetId": "cf-good"}, {"FleetId": "cf-bad"}]}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": fleets_by_id[kw["FleetId"]]},
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        rows = result["ContainerFleets"]
        # Only the valid fleet survives; the all-invalid one is discarded even
        # though the constant FleetType marker would otherwise keep it.
        assert len(rows) == 1
        assert rows[0]["Status"] == "ACTIVE"
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        assert result.get("truncated") is True
        assert result.get("partial") is True
        assert result["FleetCounts"]["Container"] == 1
        _assert_no_sensitive(result)


class TestClassicRequiresFleetId:
    def test_classic_row_without_valid_fleet_id_is_discarded(self):
        # A classic row with no grammar-valid FleetId is useless and misleading;
        # it must be discarded (and the discard surfaced), not kept on its other
        # fields.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        def describe(**kw):
            return {"FleetAttributes": [{"Status": "ACTIVE", "InstanceType": "c5.large"}]}

        fake = _FakeGameLift(
            list_fleets_pages=[{"FleetIds": ["fleet-x"]}],
            describe_fleet_attributes=describe,
        )
        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        assert result["ClassicFleets"] == []
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        # Terminal malformed listing: no rows retained, so no truncation marker.
        assert "truncated" not in result
        assert result.get("partial") is True

    def test_classic_row_with_arn_fleet_id_is_discarded(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        # An ARN in the FleetId field fails the identifier grammar, so the whole
        # row is dropped even though Status is a valid token.
        row = project_classic_fleet({"FleetId": SYNTHETIC_FLEET_ARN, "Status": "ACTIVE"})
        assert row == {}


class TestContainerMarkerDoesNotMaskInvalidRows:
    def test_all_invalid_container_fleet_is_discarded_not_marker_only(self):
        # A container summary whose every provider-derived field fails validation
        # must project to nothing. The code-owned FleetType marker alone must not
        # keep a would-be-empty row alive.
        # Local modules
        from agents.gamelift_projections import project_container_fleet_summary

        projected = project_container_fleet_summary(
            {
                "FleetType": "container",
                "Status": "arn:aws:evil",
                "InstanceType": {"nested": "blob"},
                "BillingType": SYNTHETIC_URL,
                "PlayerGatewayMode": SYNTHETIC_IP,
            }
        )
        assert projected == {}

    def test_marker_plus_one_valid_field_is_kept(self):
        # A container summary with the marker and at least one valid provider
        # field is a real fleet and is kept.
        # Local modules
        from agents.gamelift_projections import project_container_fleet_summary

        projected = project_container_fleet_summary({"FleetType": "container", "Status": "ACTIVE"})
        assert projected == {"FleetType": "container", "Status": "ACTIVE"}


class TestWarningsAreBoundedAndDeduplicated:
    def test_max_failure_fan_out_bounds_warnings_and_keeps_rows(self):
        # Drive the maximum container describe failure fan-out: every container
        # fleet's describe_container_fleet AND list_fleet_deployments fail with
        # the same codes, so without dedup the Warnings list would grow ~2-3 per
        # fleet and could crowd real rows out of the aggregate envelope.
        #
        # Warnings are deduplicated by (Source, Code) with an
        # integer Count, the number of DISTINCT entries is capped by a code-owned
        # constant, retained fleet rows survive, and the final serialized
        # envelope stays within the char budget.
        # Local modules
        from agents.gamelift_projections import (
            GAMELIFT_MAX_PROJECTED_CHARS,
            GAMELIFT_MAX_PROJECTED_ITEMS,
            GAMELIFT_MAX_WARNINGS,
        )
        from agents.gamelift_specialist import list_gamelift_fleets

        n = GAMELIFT_MAX_PROJECTED_ITEMS + 40
        listed = [{"FleetId": f"cf-{i:04d}"} for i in range(n)]

        # Every fleet describes successfully (so real rows survive), but every
        # per-fleet deployment read fails, driving a large deployment-warning
        # fan-out. Group-definition describe also fails for every fleet. Without
        # dedup this would be ~2 warnings per fleet (hundreds of entries).
        def describe_container_fleet(**kw):
            return {
                "ContainerFleet": {
                    "FleetId": kw["FleetId"],
                    "Status": "ACTIVE",
                    "InstanceType": "c6i.large",
                    "GameServerContainerGroupDefinitionName": "game-server-group",
                }
            }

        def get_paginator_wrapper(fake):
            def get_paginator(operation_name: str):
                fake.invoked_operations.append(operation_name)
                if operation_name == "list_container_fleets":
                    return _paginator([{"ContainerFleets": listed}])
                if operation_name == "list_fleets":
                    return _paginator([{"FleetIds": []}])
                if operation_name == "list_container_group_definitions":
                    return _paginator([{"ContainerGroupDefinitions": []}])
                if operation_name == "list_fleet_deployments":
                    return _paginator(error=Exception(f"deployments boom {SYNTHETIC_URL}"))
                raise AssertionError(operation_name)

            return get_paginator

        def describe_container_group_definition(**kw):
            raise Exception(f"group def boom {SYNTHETIC_FLEET_ARN}")

        fake = _FakeGameLift(
            describe_container_fleet=describe_container_fleet,
            describe_container_group_definition=describe_container_group_definition,
        )
        fake.get_paginator = get_paginator_wrapper(fake)  # type: ignore[assignment]

        with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
            result = list_gamelift_fleets()

        warnings = result["Warnings"]
        # Deduplicated: each entry is a {Source, Code, Count} triple, no provider
        # text, and the DISTINCT entries are capped by the code-owned constant.
        assert len(warnings) <= GAMELIFT_MAX_WARNINGS
        seen_keys = set()
        for warning in warnings:
            assert set(warning.keys()) == {"Source", "Code", "Count"}, f"warning shape wrong: {warning}"
            assert isinstance(warning["Count"], int) and warning["Count"] >= 1
            key = (warning["Source"], warning["Code"])
            assert key not in seen_keys, f"duplicate warning key not merged: {key}"
            seen_keys.add(key)
        # A deduplicated describe failure must have aggregated many occurrences.
        assert any(w["Count"] > 1 for w in warnings), "warnings were not aggregated with a Count"
        # Real fleet rows were NOT crowded out by warnings.
        assert len(result["ContainerFleets"]) >= 1
        # Exact aggregate budget holds on the final serialized envelope.
        assert len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS
        assert result["status"] in {"incomplete", "truncated"}
        assert result.get("partial") is True
        _assert_no_sensitive(result)
