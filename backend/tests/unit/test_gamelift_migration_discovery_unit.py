"""Unit tests for the read-only GameLift migration discovery tools (#529).

They prove the new queue, location, container-definition, and container-port
tools use explicit owned GameLift reads (no generic dispatcher), validate
identifiers before any provider call, project into bounded code-owned schemas,
keep ARNs, account IDs, registry hosts, IP ranges, env values, and provider text
out of model context and logs, and keep empty / denied / incomplete /
malformed / truncated distinct. Synthetic, public-safe fixtures only.
"""

# Standard library
import ast
import json
import pathlib
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
import agents.gamelift_migration_projections as gmp
import agents.gamelift_specialist as gls

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
FLEET = "fleet-0a1b2c3d-1111-2222-3333-4444aaaabbbb"
CFLEET = "containerfleet-0a1b2c3d-1111-2222-3333-4444aaaabbbb"
ALIAS = "alias-0a1b2c3d-1111-2222-3333-4444aaaabbbb"
QUEUE_ARN = f"arn:aws:gamelift:us-west-2:{ACCOUNT}:gamesessionqueue/synthetic-queue"
SNS_ARN = f"arn:aws:sns:us-west-2:{ACCOUNT}:synthetic-topic"
ECR_IMAGE = f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/game/echo:1.0"
SECRET = "synthetic-env-secret"
PROVIDER_TEXT = f"User: arn:aws:sts::{ACCOUNT}:assumed-role/x is not authorized"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": PROVIDER_TEXT}}, "Op")


def _queue(name: str, destinations: int = 2, dirty: bool = False) -> dict:
    """A valid queue. ``dirty`` adds an invalid priority type and Region."""
    dests = []
    for i in range(destinations):
        if i % 2:
            dests.append({"DestinationArn": f"arn:aws:gamelift:us-east-1:{ACCOUNT}:alias/{ALIAS}"})
        else:
            dests.append({"DestinationArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{FLEET}"})
    return {
        "Name": name,
        "GameSessionQueueArn": QUEUE_ARN,
        "TimeoutInSeconds": 600,
        "Destinations": dests,
        "PlayerLatencyPolicies": [{"MaximumIndividualPlayerLatencyMilliseconds": 100, "PolicyDurationSeconds": 30}],
        "PriorityConfiguration": {
            "PriorityOrder": ["LATENCY", "COST", "BOGUS"] if dirty else ["LATENCY", "COST"],
            "LocationOrder": ["us-west-2", "10.0.0.1"] if dirty else ["us-west-2"],
        },
        "FilterConfiguration": {"AllowedLocations": ["us-west-2", "us-east-1"]},
        "CustomEventData": SECRET,
        "NotificationTarget": SNS_ARN,
    }


def _client_with_pages(pages):
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = pages
    return client


# --------------------------------------------------------------------------- #
# No generic dispatcher                                                       #
# --------------------------------------------------------------------------- #
def test_registered_tools_are_explicit_named_reads():
    names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in gls.GAMELIFT_AGENT_TOOLS]
    assert names == [
        "list_gamelift_fleets",
        "get_fleet_utilization",
        "get_fleet_capacity",
        "get_scaling_policies",
        "list_game_session_queues",
        "get_fleet_locations",
        "get_container_group_definition",
        "get_container_fleet_ports",
    ]


def test_specialist_calls_only_reviewed_read_operations_and_no_dynamic_dispatch():
    """Static check: every boto3 client method called in the specialist is a
    reviewed List/Describe read, and nothing dispatches by computed name."""
    source = pathlib.Path(gls.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reviewed = {
        "describe_fleet_attributes",
        "describe_container_fleet",
        "describe_container_group_definition",
        "describe_fleet_utilization",
        "describe_fleet_capacity",
        "describe_scaling_policies",
        "describe_fleet_location_attributes",
        "get_paginator",
    }
    paginated = {
        "list_fleets",
        "list_container_fleets",
        "list_container_group_definitions",
        "list_fleet_deployments",
        "describe_game_session_queues",
    }
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "client":
                called.add(node.func.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr",):
            first = node.args[0] if node.args else None
            assert not (isinstance(first, ast.Name) and first.id == "client"), "dynamic client dispatch"
    assert called <= reviewed, called - reviewed
    assert {"describe_fleet_location_attributes", "describe_container_group_definition", "get_paginator"} <= called
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert paginated <= literals
    for verb in ("create_", "update_", "delete_", "put_", "start_", "stop_", "request_", "register_", "terminate_"):
        assert not any(name.startswith(verb) for name in called)


# --------------------------------------------------------------------------- #
# Queues                                                                      #
# --------------------------------------------------------------------------- #
def test_queue_projection_derives_ids_and_regions_and_drops_arns():
    client = _client_with_pages([{"GameSessionQueues": [_queue("synthetic-queue")]}])
    with patch.object(gls.boto3, "client", return_value=client):
        clean = gls.list_game_session_queues()
    assert clean["status"] == "ok" and "error" not in clean and "truncated" not in clean

    client = _client_with_pages([{"GameSessionQueues": [_queue("synthetic-queue", dirty=True)]}])
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.list_game_session_queues()
    # The invalid priority type and the IP-shaped Region are dropped, so the
    # collection is not ok.
    assert result["status"] == "incomplete" and result["error"] == {"code": "malformed_response"}
    row = result["GameSessionQueues"][0]
    assert row["Destinations"] == [
        {"Type": "fleet", "FleetId": FLEET, "Region": "us-west-2"},
        {"Type": "alias", "AliasId": ALIAS, "Region": "us-east-1"},
    ]
    assert row["PriorityConfiguration"] == {
        "PriorityOrder": ["LATENCY", "COST"],
        "LocationOrder": ["us-west-2"],
    }
    blob = json.dumps(result)
    for forbidden in (ACCOUNT, "arn:", SECRET, "sns", "10.0.0.1"):
        assert forbidden not in blob


def test_unparsable_destination_is_counted_not_guessed():
    queue = _queue("q")
    queue["Destinations"] = [
        {"DestinationArn": "fleet-guess"},
        {"DestinationArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{FLEET}"},
    ]
    row = gmp.project_queue_item(queue)
    assert row["UnresolvedDestinationCount"] == 1
    assert len(row["Destinations"]) == 1


def test_queue_destination_and_policy_caps():
    queue = _queue("q", destinations=gmp.MAX_QUEUE_DESTINATIONS + 4)
    queue["PlayerLatencyPolicies"] = [{"PolicyDurationSeconds": i} for i in range(gmp.MAX_LATENCY_POLICIES + 3)]
    queue["FilterConfiguration"]["AllowedLocations"] = ["us-west-2"] * (gmp.MAX_QUEUE_LOCATIONS + 2)
    row = gmp.project_queue_item(queue)
    assert len(row["Destinations"]) == gmp.MAX_QUEUE_DESTINATIONS and row["DestinationsTruncated"] is True
    assert (
        len(row["PlayerLatencyPolicies"]) == gmp.MAX_LATENCY_POLICIES and row["PlayerLatencyPoliciesTruncated"] is True
    )
    assert len(row["FilterConfiguration"]["AllowedLocations"]) == gmp.MAX_QUEUE_LOCATIONS


def test_queue_item_cap_is_truncated_and_paginated():
    client = _client_with_pages([{"GameSessionQueues": [_queue(f"q{i}") for i in range(gmp.MAX_QUEUES + 1)]}])
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.list_game_session_queues()
    client.get_paginator.return_value.paginate.assert_called_once_with(
        PaginationConfig={"MaxItems": gmp.MAX_QUEUES + 1}
    )
    assert result["status"] == "truncated" and result["paginated"] is True
    assert len(result["GameSessionQueues"]) == gmp.MAX_QUEUES


def test_queue_empty_denied_unavailable_malformed_are_distinct():
    client = _client_with_pages([{"GameSessionQueues": []}])
    with patch.object(gls.boto3, "client", return_value=client):
        assert gls.list_game_session_queues()["status"] == "empty"

    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = _client_error("AccessDeniedException")
    with patch.object(gls.boto3, "client", return_value=client):
        denied = gls.list_game_session_queues()
    assert denied["status"] == "denied" and denied["error"] == {"code": "access_denied"}

    client.get_paginator.return_value.paginate.side_effect = _client_error("ThrottlingException")
    with patch.object(gls.boto3, "client", return_value=client):
        throttled = gls.list_game_session_queues()
    assert throttled["status"] == "incomplete" and throttled["error"] == {"code": "throttled"}

    client = _client_with_pages([{"GameSessionQueues": "not-a-list"}])
    with patch.object(gls.boto3, "client", return_value=client):
        malformed = gls.list_game_session_queues()
    assert malformed["status"] == "incomplete" and malformed["error"] == {"code": "malformed_response"}


def test_queue_serialized_budget(monkeypatch):
    monkeypatch.setattr("agents.gamelift_projections.GAMELIFT_MAX_PROJECTED_CHARS", 1_500)
    queues = [_queue(f"queue-{i}", destinations=10) for i in range(gmp.MAX_QUEUES)]
    result = gmp.project_game_session_queues(queues, residual=False, pages_malformed=False)
    assert len(json.dumps(result)) <= 1_500
    assert result["status"] == "truncated" and result["truncated"] is True


# --------------------------------------------------------------------------- #
# Locations                                                                   #
# --------------------------------------------------------------------------- #
def test_classic_locations():
    client = MagicMock()
    client.describe_fleet_location_attributes.return_value = {
        "FleetId": FLEET,
        "FleetArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{FLEET}",
        "LocationAttributes": [
            {"LocationState": {"Location": "us-west-2", "Status": "ACTIVE"}, "UpdateStatus": "PENDING_UPDATE"},
            {"LocationState": {"Location": "eu-west-1", "Status": "ACTIVATING", "PlayerGatewayStatus": "ENABLED"}},
        ],
    }
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_fleet_locations(FLEET)
    client.describe_fleet_location_attributes.assert_called_once_with(FleetId=FLEET, Limit=gmp.MAX_FLEET_LOCATIONS)
    assert result["status"] == "ok" and result["FleetId"] == FLEET
    assert result["Locations"] == [
        {"Location": "us-west-2", "Status": "ACTIVE", "UpdateStatus": "PENDING_UPDATE"},
        {"Location": "eu-west-1", "Status": "ACTIVATING", "PlayerGatewayStatus": "ENABLED"},
    ]
    assert ACCOUNT not in json.dumps(result)


def test_classic_locations_next_token_is_incomplete():
    client = MagicMock()
    client.describe_fleet_location_attributes.return_value = {
        "LocationAttributes": [{"LocationState": {"Location": "us-west-2", "Status": "ACTIVE"}}],
        "NextToken": "more",
    }
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_fleet_locations(FLEET)
    assert result["status"] == "incomplete" and result["truncated"] is True


def test_container_locations_use_describe_container_fleet():
    client = MagicMock()
    client.describe_container_fleet.return_value = {
        "ContainerFleet": {"FleetId": CFLEET, "LocationAttributes": [{"Location": "us-west-2", "Status": "ACTIVE"}]}
    }
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_fleet_locations(CFLEET)
    client.describe_fleet_location_attributes.assert_not_called()
    assert result["Locations"] == [{"Location": "us-west-2", "Status": "ACTIVE"}]


@pytest.mark.parametrize(
    "bad", [f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{FLEET}", "fleet-1;rm", "", ACCOUNT, "x" * 300]
)
def test_invalid_fleet_id_rejected_before_provider_call(bad):
    client = MagicMock()
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_fleet_locations(bad)
        ports = gls.get_container_fleet_ports(bad)
    assert result["error"] == {"code": "invalid_request"} and ports["error"] == {"code": "invalid_request"}
    client.describe_fleet_location_attributes.assert_not_called()
    client.describe_container_fleet.assert_not_called()


def test_locations_denied_and_empty():
    client = MagicMock()
    client.describe_fleet_location_attributes.side_effect = _client_error("UnauthorizedException")
    with patch.object(gls.boto3, "client", return_value=client):
        assert gls.get_fleet_locations(FLEET)["status"] == "denied"
    client = MagicMock()
    client.describe_fleet_location_attributes.return_value = {"LocationAttributes": []}
    with patch.object(gls.boto3, "client", return_value=client):
        assert gls.get_fleet_locations(FLEET)["status"] == "empty"


# --------------------------------------------------------------------------- #
# Container group definitions                                                 #
# --------------------------------------------------------------------------- #
def _definition(support: int = 1, port_ranges: int = 1) -> dict:
    def container(name: str) -> dict:
        return {
            "ContainerName": name,
            "ImageUri": ECR_IMAGE,
            "ResolvedImageDigest": "sha256:" + "a" * 64,
            "EnvironmentOverride": [{"Name": "TOKEN", "Value": SECRET}],
            "MountPoints": [{"InstancePath": "/secret/path", "ContainerPath": "/data"}],
            "PortConfiguration": {
                "ContainerPortRanges": [
                    {"FromPort": 7000 + i, "ToPort": 7000 + i, "Protocol": "UDP"} for i in range(port_ranges)
                ]
            },
        }

    game = container("game")
    game["ServerSdkVersion"] = "5.2.0"
    support_rows = []
    for i in range(support):
        row = container(f"sidecar{i}")
        row["HealthCheck"] = {"Command": ["curl", "http://localhost/secret"]}
        row["Essential"] = True
        row["MemoryHardLimitMebibytes"] = 256
        support_rows.append(row)
    return {
        "ContainerGroupDefinition": {
            "ContainerGroupDefinitionArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:containergroupdefinition/echo:1",
            "Name": "echo",
            "VersionNumber": 1,
            "ContainerGroupType": "GAME_SERVER",
            "Status": "READY",
            "StatusReason": PROVIDER_TEXT,
            "VersionDescription": "free text " + SECRET,
            "OperatingSystem": "AMAZON_LINUX_2023",
            "TotalMemoryLimitMebibytes": 1024,
            "TotalVcpuLimit": 1.0,
            "GameServerContainerDefinition": game,
            "SupportContainerDefinitions": support_rows,
        }
    }


def test_container_group_definition_projection():
    client = MagicMock()
    client.describe_container_group_definition.return_value = _definition()
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_container_group_definition("echo", 1)
    client.describe_container_group_definition.assert_called_once_with(Name="echo", VersionNumber=1)
    assert result["status"] == "ok"
    cgd = result["ContainerGroupDefinition"]
    game = cgd["GameServerContainerDefinition"]
    assert game["Image"] == {
        "registryType": "ecr-private",
        "registryRegion": "us-west-2",
        "repository": "game/echo",
        "tag": "1.0",
        "complete": True,
    }
    assert game["PortRanges"] == [{"FromPort": 7000, "ToPort": 7000, "Protocol": "UDP"}]
    assert game["ServerSdkVersion"] == "5.2.0"
    blob = json.dumps(result)
    for forbidden in (ACCOUNT, "arn:", SECRET, "TOKEN", "/secret", "localhost", "amazonaws.com", "not authorized"):
        assert forbidden not in blob


def test_container_definition_nested_caps_and_budget(monkeypatch):
    raw = _definition(support=gmp.MAX_SUPPORT_CONTAINERS + 3, port_ranges=gmp.MAX_PORT_RANGES + 2)
    result = gmp.project_container_group_definition(raw)
    cgd = result["ContainerGroupDefinition"]
    assert len(cgd["SupportContainerDefinitions"]) == gmp.MAX_SUPPORT_CONTAINERS
    assert len(cgd["GameServerContainerDefinition"]["PortRanges"]) == gmp.MAX_PORT_RANGES
    assert result["status"] == "truncated" and result["truncated"] is True

    monkeypatch.setattr(gmp, "GAMELIFT_MAX_PROJECTED_CHARS", 900)
    small = gmp.project_container_group_definition(raw)
    assert len(json.dumps(small)) <= 900 and small["status"] == "truncated"


@pytest.mark.parametrize(
    "name", [f"arn:aws:gamelift:us-west-2:{ACCOUNT}:containergroupdefinition/echo", "bad name", "", "a" * 200]
)
def test_container_definition_name_validated_before_call(name):
    client = MagicMock()
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_container_group_definition(name)
    assert result["error"] == {"code": "invalid_request"}
    client.describe_container_group_definition.assert_not_called()


def test_container_definition_malformed_and_denied():
    client = MagicMock()
    client.describe_container_group_definition.return_value = {"ContainerGroupDefinition": None}
    with patch.object(gls.boto3, "client", return_value=client):
        assert gls.get_container_group_definition("echo")["error"] == {"code": "malformed_response"}
    client.describe_container_group_definition.side_effect = _client_error("AccessDeniedException")
    with patch.object(gls.boto3, "client", return_value=client):
        assert gls.get_container_group_definition("echo")["status"] == "denied"


# --------------------------------------------------------------------------- #
# Container fleet ports                                                       #
# --------------------------------------------------------------------------- #
def test_container_fleet_ports_drop_ip_ranges_and_arns():
    client = MagicMock()
    client.describe_container_fleet.return_value = {
        "ContainerFleet": {
            "FleetId": CFLEET,
            "FleetArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{CFLEET}",
            "FleetRoleArn": f"arn:aws:iam::{ACCOUNT}:role/x",
            "GameServerContainerGroupDefinitionName": "echo",
            "GameServerContainerGroupsPerInstance": 4,
            "BillingType": "ON_DEMAND",
            "InstanceConnectionPortRange": {"FromPort": 4192, "ToPort": 4200},
            "InstanceInboundPermissions": [
                {"FromPort": 4192, "ToPort": 4200, "IpRange": "0.0.0.0/0", "Protocol": "UDP"},
                {"FromPort": 4192, "ToPort": 4200, "IpRange": "203.0.113.0/24", "Protocol": "TCP"},
            ],
        }
    }
    with patch.object(gls.boto3, "client", return_value=client):
        result = gls.get_container_fleet_ports(CFLEET)
    fleet = result["ContainerFleet"]
    assert fleet["InstanceInboundPermissions"] == [
        {"FromPort": 4192, "ToPort": 4200, "Protocol": "UDP", "Scope": "any-address"},
        {"FromPort": 4192, "ToPort": 4200, "Protocol": "TCP", "Scope": "restricted"},
    ]
    blob = json.dumps(result)
    for forbidden in ("0.0.0.0", "203.0.113", ACCOUNT, "arn:"):
        assert forbidden not in blob


def test_container_fleet_ports_inbound_cap():
    permissions = [{"FromPort": 1000, "ToPort": 1000, "Protocol": "UDP"}] * (gmp.MAX_INBOUND_PERMISSIONS + 1)
    result = gmp.project_container_fleet_ports(
        {"ContainerFleet": {"FleetId": CFLEET, "InstanceInboundPermissions": permissions}}
    )
    assert len(result["ContainerFleet"]["InstanceInboundPermissions"]) == gmp.MAX_INBOUND_PERMISSIONS
    assert result["status"] == "truncated"


def test_classic_fleet_id_rejected_for_container_ports():
    client = MagicMock()
    with patch.object(gls.boto3, "client", return_value=client):
        assert gls.get_container_fleet_ports(FLEET)["error"] == {"code": "invalid_request"}
    client.describe_container_fleet.assert_not_called()


# --------------------------------------------------------------------------- #
# Logs                                                                        #
# --------------------------------------------------------------------------- #
def test_provider_text_never_logged():
    # Local modules
    from utils.logger import logger

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m) + json.dumps(m.record["extra"], default=str)), level="DEBUG")
    try:
        client = MagicMock()
        client.describe_container_group_definition.side_effect = _client_error("AccessDeniedException")
        client.get_paginator.return_value.paginate.side_effect = _client_error("AccessDeniedException")
        client.describe_fleet_location_attributes.side_effect = _client_error("AccessDeniedException")
        with patch.object(gls.boto3, "client", return_value=client):
            gls.get_container_group_definition("echo")
            gls.list_game_session_queues()
            gls.get_fleet_locations(FLEET)
    finally:
        logger.remove(sink)
    joined = "\n".join(messages)
    assert messages
    assert ACCOUNT not in joined and "assumed-role" not in joined and FLEET not in joined


# --------------------------------------------------------------------------- #
# Nested caps and drops never leave a collection ok (#781 review item 4)      #
# --------------------------------------------------------------------------- #
def _queues_status(queue: dict) -> dict:
    return gmp.project_game_session_queues([queue], residual=False, pages_malformed=False)


def _assert_truncated(result: dict) -> None:
    assert result["status"] == "truncated" and result["truncated"] is True
    assert "error" not in result


def _assert_malformed(result: dict) -> None:
    assert result["status"] == "incomplete" and result["truncated"] is True
    assert result["error"] == {"code": "malformed_response"}


def test_queue_with_24_destinations_is_truncated():
    result = _queues_status(_queue("q", destinations=24))
    assert len(result["GameSessionQueues"][0]["Destinations"]) == gmp.MAX_QUEUE_DESTINATIONS
    _assert_truncated(result)


def test_queue_with_an_unparsable_destination_is_incomplete_malformed():
    queue = _queue("q")
    queue["Destinations"].append({"DestinationArn": "fleet-guess"})
    result = _queues_status(queue)
    assert result["GameSessionQueues"][0]["UnresolvedDestinationCount"] == 1
    _assert_malformed(result)


def test_queue_with_25_entry_location_order_is_truncated():
    queue = _queue("q")
    queue["PriorityConfiguration"]["LocationOrder"] = ["us-west-2"] * 25
    result = _queues_status(queue)
    assert result["GameSessionQueues"][0]["PriorityConfiguration"]["LocationOrderTruncated"] is True
    _assert_truncated(result)


def test_queue_with_too_many_allowed_locations_or_policies_is_truncated():
    queue = _queue("q")
    queue["FilterConfiguration"]["AllowedLocations"] = ["us-west-2"] * (gmp.MAX_QUEUE_LOCATIONS + 1)
    _assert_truncated(_queues_status(queue))

    queue = _queue("q")
    queue["PlayerLatencyPolicies"] = [{"PolicyDurationSeconds": 30}] * (gmp.MAX_LATENCY_POLICIES + 1)
    _assert_truncated(_queues_status(queue))


@pytest.mark.parametrize(
    "policies",
    [
        ["not-a-mapping"],
        [{"MaximumIndividualPlayerLatencyMilliseconds": "fast"}],
        [{"MaximumIndividualPlayerLatencyMilliseconds": 100, "PolicyDurationSeconds": float("nan")}],
        [{"MaximumIndividualPlayerLatencyMilliseconds": 100}, {"PolicyDurationSeconds": True}],
    ],
)
def test_queue_with_an_invalid_latency_policy_is_incomplete_malformed(policies):
    queue = _queue("q")
    queue["PlayerLatencyPolicies"] = policies
    _assert_malformed(_queues_status(queue))


@pytest.mark.parametrize(
    "priority",
    [
        {"PriorityOrder": ["LATENCY", "BOGUS"]},
        {"LocationOrder": ["us-west-2", "10.0.0.1"]},
        {"LocationOrder": "us-west-2"},
    ],
)
def test_queue_with_an_invalid_priority_entry_is_incomplete_malformed(priority):
    queue = _queue("q")
    queue["PriorityConfiguration"] = priority
    _assert_malformed(_queues_status(queue))


def test_queue_with_an_invalid_allowed_location_is_incomplete_malformed():
    queue = _queue("q")
    queue["FilterConfiguration"]["AllowedLocations"] = ["us-west-2", "not a region"]
    _assert_malformed(_queues_status(queue))


def test_a_drop_wins_over_a_cap():
    queue = _queue("q", destinations=24)
    queue["Destinations"].insert(0, {"DestinationArn": "fleet-guess"})
    _assert_malformed(_queues_status(queue))


def test_nested_drop_keeps_the_envelope_within_budget(monkeypatch):
    monkeypatch.setattr("agents.gamelift_projections.GAMELIFT_MAX_PROJECTED_CHARS", 1_500)
    monkeypatch.setattr(gmp, "GAMELIFT_MAX_PROJECTED_CHARS", 1_500)
    queues = [_queue(f"queue-{i}", destinations=10, dirty=True) for i in range(gmp.MAX_QUEUES)]
    result = gmp.project_game_session_queues(queues, residual=False, pages_malformed=False)
    assert len(json.dumps(result)) <= 1_500
    _assert_malformed(result)


@pytest.mark.parametrize(
    "bad_range",
    [
        {"FromPort": 0, "ToPort": 7000, "Protocol": "UDP"},
        {"FromPort": 7001, "ToPort": 7000, "Protocol": "UDP"},
        {"FromPort": 7000, "ToPort": 70000, "Protocol": "UDP"},
        {"FromPort": 7000, "ToPort": 7000, "Protocol": "ICMP"},
        "not-a-mapping",
    ],
)
def test_container_definition_with_an_invalid_port_range_is_incomplete_malformed(bad_range):
    raw = _definition()
    ranges = raw["ContainerGroupDefinition"]["GameServerContainerDefinition"]["PortConfiguration"][
        "ContainerPortRanges"
    ]
    ranges.append(bad_range)
    result = gmp.project_container_group_definition(raw)
    assert result["ContainerGroupDefinition"]["GameServerContainerDefinition"]["PortRanges"] == [
        {"FromPort": 7000, "ToPort": 7000, "Protocol": "UDP"}
    ]
    _assert_malformed(result)


def test_container_definition_with_an_unprojectable_support_container_is_incomplete_malformed():
    raw = _definition(support=1)
    raw["ContainerGroupDefinition"]["SupportContainerDefinitions"].append("not-a-mapping")
    _assert_malformed(gmp.project_container_group_definition(raw))


def test_container_definition_port_range_cap_alone_is_truncated():
    raw = _definition(port_ranges=gmp.MAX_PORT_RANGES + 1)
    _assert_truncated(gmp.project_container_group_definition(raw))


def test_clean_container_definition_is_ok():
    result = gmp.project_container_group_definition(_definition())
    assert result["status"] == "ok" and "truncated" not in result and "error" not in result


@pytest.mark.parametrize(
    "fleet_extra",
    [
        {"InstanceInboundPermissions": [{"FromPort": 4200, "ToPort": 4192, "Protocol": "UDP"}]},
        {"InstanceInboundPermissions": [{"FromPort": 4192, "ToPort": 4200, "Protocol": "ICMP"}]},
        {"InstanceInboundPermissions": ["not-a-mapping"]},
        {"InstanceInboundPermissions": "not-a-list"},
        {"InstanceConnectionPortRange": {"FromPort": 0, "ToPort": 4200}},
        {"InstanceConnectionPortRange": "4192-4200"},
    ],
)
def test_container_fleet_ports_with_an_invalid_port_entry_is_incomplete_malformed(fleet_extra):
    fleet = {
        "FleetId": CFLEET,
        "InstanceInboundPermissions": [{"FromPort": 4192, "ToPort": 4200, "Protocol": "UDP"}],
        **fleet_extra,
    }
    _assert_malformed(gmp.project_container_fleet_ports({"ContainerFleet": fleet}))


def test_container_fleet_ports_inbound_cap_alone_is_truncated():
    permissions = [{"FromPort": 1000, "ToPort": 1000, "Protocol": "UDP"}] * (gmp.MAX_INBOUND_PERMISSIONS + 1)
    result = gmp.project_container_fleet_ports(
        {"ContainerFleet": {"FleetId": CFLEET, "InstanceInboundPermissions": permissions}}
    )
    _assert_truncated(result)


def test_clean_container_fleet_ports_are_ok():
    result = gmp.project_container_fleet_ports(
        {
            "ContainerFleet": {
                "FleetId": CFLEET,
                "InstanceConnectionPortRange": {"FromPort": 4192, "ToPort": 4200},
                "InstanceInboundPermissions": [{"FromPort": 4192, "ToPort": 4200, "Protocol": "UDP"}],
            }
        }
    )
    assert result["status"] == "ok" and "truncated" not in result and "error" not in result
