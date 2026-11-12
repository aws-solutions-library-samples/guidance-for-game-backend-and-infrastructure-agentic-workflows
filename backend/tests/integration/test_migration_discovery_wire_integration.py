"""Offline wire-integration tests for read-only migration discovery (#529).

No AWS or cluster call is made.

* EKS / Agones: the installed ``awslabs.eks_mcp_server`` ``K8sHandler`` runs
  in its default read-only mode (no ``allow_write``, no sensitive-data access)
  against an in-memory Kubernetes API built from real
  ``kubernetes.dynamic.ResourceInstance`` objects. The handler produces its
  real ``CallToolResult``, which is mapped to the Strands tool-result shape and
  consumed through ``EksMcpGuardedProvider``. This proves the inventory parses
  the server's actual wire format and that only READ reaches the Kubernetes
  client.
* GameLift: the new tools run against a real boto3 client with
  ``botocore.stub.Stubber``, which validates both the request parameters and the
  stubbed responses against the pinned GameLift service model.
"""

# Standard library
import asyncio
import json
from types import SimpleNamespace
from typing import Any, Sequence
from unittest.mock import MagicMock, patch

# Third-party packages
import boto3
import pytest
from awslabs.eks_mcp_server.k8s_handler import K8sHandler
from awslabs.eks_mcp_server.models import Operation
from botocore.config import Config
from botocore.stub import Stubber
from kubernetes.dynamic.resource import ResourceInstance
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
import agents.agones_discovery as ad
from agents.eks_mcp_guard import guard_eks_mcp_client

pytestmark = pytest.mark.integration

ACCOUNT = "123456789012"
CLUSTER = "synthetic-games"
FLEET_ID = "fleet-0a1b2c3d-1111-2222-3333-4444aaaabbbb"
CFLEET_ID = "containerfleet-0a1b2c3d-1111-2222-3333-4444aaaabbbb"


# --------------------------------------------------------------------------- #
# EKS MCP handler wire                                                        #
# --------------------------------------------------------------------------- #
class _InMemoryK8s:
    """Minimal stand-in for the handler's ``K8sApis`` client."""

    def __init__(self, objects: dict[str, list[dict[str, Any]]]) -> None:
        self.objects = objects
        self.operations: list[Operation] = []

    def list_resources(self, kind, api_version, namespace=None, label_selector=None, field_selector=None):
        items = [ResourceInstance(None, obj) for obj in self.objects.get(kind, [])]
        return SimpleNamespace(items=items)

    def manage_resource(self, operation, kind, api_version, name=None, namespace=None, body=None):
        self.operations.append(operation)
        for obj in self.objects.get(kind, []):
            if obj["metadata"]["name"] == name:
                return ResourceInstance(None, obj)
        raise RuntimeError("(404) Reason: Not Found")


class _HandlerTool(AgentTool):
    """Expose one real handler coroutine as a Strands-shaped MCP tool."""

    _DEFAULTS = {
        "list_k8s_resources": {"namespace": None, "label_selector": None, "field_selector": None},
        "manage_k8s_resource": {"name": None, "namespace": None, "body": None},
        "apply_yaml": {},
    }

    def __init__(self, handler: K8sHandler, name: str) -> None:
        super().__init__()
        self._handler = handler
        self._name = name

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": "real", "inputSchema": {"json": {"type": "object"}}}

    @property
    def tool_type(self) -> str:
        return "mcp"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        params = {**self._DEFAULTS[self._name], **tool_use["input"]}
        ctx = SimpleNamespace(request_id="synthetic")
        result = await getattr(self._handler, self._name)(ctx, **params)
        yield ToolResultEvent(
            {
                "toolUseId": tool_use["toolUseId"],
                "status": "error" if result.isError else "success",
                "content": [{"text": block.text} for block in result.content],
            }
        )


class _Provider(ToolProvider):
    def __init__(self, tools: Sequence[AgentTool]) -> None:
        self.tools = tools

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        return self.tools

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass


def _agones_objects() -> dict[str, list[dict[str, Any]]]:
    fleet = {
        "apiVersion": "agones.dev/v1",
        "kind": "Fleet",
        "metadata": {
            "name": "simple-game-server",
            "namespace": "default",
            "creationTimestamp": "2026-10-01T00:00:00Z",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": '{"secret":"x"}'},
            "managedFields": [{"manager": "kubectl"}],
        },
        "spec": {
            "replicas": 3,
            "scheduling": "Packed",
            "template": {
                "spec": {
                    "ports": [{"name": "default", "portPolicy": "Dynamic", "containerPort": 7654, "protocol": "UDP"}],
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "simple-game-server",
                                    "image": "us-docker.pkg.dev/agones-images/examples/simple-game-server:0.43",
                                    "env": [{"name": "TOKEN", "value": "synthetic"}],
                                }
                            ]
                        }
                    },
                }
            },
        },
        "status": {"replicas": 3, "readyReplicas": 3, "allocatedReplicas": 0, "reservedReplicas": 0},
    }
    gameserver = {
        "apiVersion": "agones.dev/v1",
        "kind": "GameServer",
        "metadata": {
            "name": "simple-game-server-abcde",
            "namespace": "default",
            "labels": {"agones.dev/fleet": "simple-game-server"},
        },
        "status": {
            "state": "Ready",
            "address": "10.0.1.20",
            "nodeName": "ip-10-0-1-20.internal",
            "ports": [{"name": "default", "port": 7400}],
        },
    }
    return {"Fleet": [fleet], "GameServer": [gameserver], "FleetAutoscaler": []}


def _run_inventory(k8s: _InMemoryK8s) -> tuple[dict[str, Any], str]:
    handler = K8sHandler(MagicMock())
    assert handler.allow_write is False and handler.allow_sensitive_data_access is False
    handler.get_client = lambda cluster_name: k8s  # type: ignore[method-assign]
    delegates = [_HandlerTool(handler, n) for n in ("list_k8s_resources", "manage_k8s_resource", "apply_yaml")]
    tools = list(asyncio.run(guard_eks_mcp_client("eks-mcp-server", _Provider(delegates)).load_tools()))
    assert {t.tool_name for t in tools} == {"list_k8s_resources", "get_agones_inventory"}
    tool = next(t for t in tools if t.tool_name == "get_agones_inventory")

    async def _go():
        final = None
        async for event in tool.stream(
            {"toolUseId": "tu", "name": tool.tool_name, "input": {"cluster_name": CLUSTER}}, {}
        ):
            if isinstance(event, ToolResultEvent):
                final = event.tool_result
        return final

    result = asyncio.run(_go())
    text = result["content"][0]["text"]
    return json.loads(text), text


def test_agones_inventory_over_real_eks_mcp_handler(monkeypatch):
    monkeypatch.setattr(ad.settings, "EKS_DISCOVERY_CLUSTERS_RAW", CLUSTER, raising=False)
    k8s = _InMemoryK8s(_agones_objects())
    body, text = _run_inventory(k8s)
    assert body["outcome"] == "complete"
    fleet = body["fleets"]["items"][0]
    assert fleet["name"] == "simple-game-server" and fleet["replicas"] == 3
    assert fleet["template"]["ports"][0]["containerPort"] == 7654
    assert fleet["template"]["containers"][0]["image"]["repository"] == "agones-images/examples/simple-game-server"
    assert body["gameServers"]["stateCounts"] == {"Ready": 1}
    assert body["gameServers"]["items"][0]["statusPorts"] == [{"name": "default", "port": 7400}]
    assert set(k8s.operations) == {Operation.READ}
    for forbidden in ("10.0.1.20", "ip-10-0-1-20", "TOKEN", "last-applied", "managedFields", "pkg.dev"):
        assert forbidden not in text


def test_agones_stale_and_denied_over_real_handler(monkeypatch):
    monkeypatch.setattr(ad.settings, "EKS_DISCOVERY_CLUSTERS_RAW", CLUSTER, raising=False)

    class _Racy(_InMemoryK8s):
        def manage_resource(self, operation, kind, api_version, name=None, namespace=None, body=None):
            self.operations.append(operation)
            raise RuntimeError("(404) Reason: Not Found")

    body, _ = _run_inventory(_Racy(_agones_objects()))
    assert body["fleets"]["stale"] is True and body["stale"] is True

    class _Forbidden(_InMemoryK8s):
        def list_resources(self, *args, **kwargs):
            raise RuntimeError(f"(403) fleets.agones.dev is forbidden: User arn:aws:sts::{ACCOUNT}:assumed-role/x")

    body, text = _run_inventory(_Forbidden(_agones_objects()))
    assert body["outcome"] == "denied"
    assert ACCOUNT not in text and "assumed-role" not in text


# --------------------------------------------------------------------------- #
# GameLift Stubber wire                                                       #
# --------------------------------------------------------------------------- #
def _stubbed():
    client = boto3.client(
        "gamelift",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(retries={"max_attempts": 0}),
    )
    return client, Stubber(client)


def test_queues_against_service_model():
    # Local modules
    from agents.gamelift_specialist import list_game_session_queues

    client, stubber = _stubbed()
    stubber.add_response(
        "describe_game_session_queues",
        {
            "GameSessionQueues": [
                {
                    "Name": "synthetic-queue",
                    "GameSessionQueueArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:gamesessionqueue/synthetic-queue",
                    "TimeoutInSeconds": 600,
                    "Destinations": [{"DestinationArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{CFLEET_ID}"}],
                    "PriorityConfiguration": {"PriorityOrder": ["LATENCY", "COST"], "LocationOrder": ["us-west-2"]},
                    "NotificationTarget": f"arn:aws:sns:us-west-2:{ACCOUNT}:topic",
                }
            ]
        },
        {},
    )
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        result = list_game_session_queues()
    assert result["status"] == "ok"
    assert result["GameSessionQueues"][0]["Destinations"] == [
        {"Type": "fleet", "FleetId": CFLEET_ID, "Region": "us-west-2"}
    ]
    assert ACCOUNT not in json.dumps(result)


def test_fleet_locations_against_service_model():
    # Local modules
    from agents.gamelift_specialist import get_fleet_locations

    client, stubber = _stubbed()
    stubber.add_response(
        "describe_fleet_location_attributes",
        {
            "FleetId": FLEET_ID,
            "FleetArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{FLEET_ID}",
            "LocationAttributes": [{"LocationState": {"Location": "us-west-2", "Status": "ACTIVE"}}],
        },
        {"FleetId": FLEET_ID, "Limit": 100},
    )
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        result = get_fleet_locations(FLEET_ID)
    assert result["Locations"] == [{"Location": "us-west-2", "Status": "ACTIVE"}]
    assert ACCOUNT not in json.dumps(result)


def test_container_definition_and_ports_against_service_model():
    # Local modules
    from agents.gamelift_specialist import get_container_fleet_ports, get_container_group_definition

    client, stubber = _stubbed()
    stubber.add_response(
        "describe_container_group_definition",
        {
            "ContainerGroupDefinition": {
                "ContainerGroupDefinitionArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:containergroupdefinition/echo:1",
                "Name": "echo",
                "VersionNumber": 1,
                "ContainerGroupType": "GAME_SERVER",
                "OperatingSystem": "AMAZON_LINUX_2023",
                "Status": "READY",
                "GameServerContainerDefinition": {
                    "ContainerName": "game",
                    "ImageUri": f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/echo:1",
                    "ServerSdkVersion": "5.2.0",
                    "EnvironmentOverride": [{"Name": "TOKEN", "Value": "synthetic"}],
                    "PortConfiguration": {
                        "ContainerPortRanges": [{"FromPort": 7654, "ToPort": 7654, "Protocol": "UDP"}]
                    },
                },
            }
        },
        {"Name": "echo"},
    )
    stubber.add_response(
        "describe_container_fleet",
        {
            "ContainerFleet": {
                "FleetId": CFLEET_ID,
                "FleetArn": f"arn:aws:gamelift:us-west-2:{ACCOUNT}:fleet/{CFLEET_ID}",
                "InstanceConnectionPortRange": {"FromPort": 4192, "ToPort": 4200},
                "InstanceInboundPermissions": [
                    {"FromPort": 4192, "ToPort": 4200, "IpRange": "0.0.0.0/0", "Protocol": "UDP"}
                ],
            }
        },
        {"FleetId": CFLEET_ID},
    )
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        definition = get_container_group_definition("echo")
        ports = get_container_fleet_ports(CFLEET_ID)
    blob = json.dumps([definition, ports])
    assert definition["ContainerGroupDefinition"]["GameServerContainerDefinition"]["Image"]["repository"] == "echo"
    assert ports["ContainerFleet"]["InstanceInboundPermissions"][0]["Scope"] == "any-address"
    for forbidden in (ACCOUNT, "TOKEN", "0.0.0.0", "arn:"):
        assert forbidden not in blob


def test_denied_against_service_model():
    # Local modules
    from agents.gamelift_specialist import list_game_session_queues

    client, stubber = _stubbed()
    stubber.add_client_error(
        "describe_game_session_queues",
        service_error_code="UnauthorizedException",
        service_message=f"arn:aws:iam::{ACCOUNT}:role/x is not authorized",
    )
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        result = list_game_session_queues()
    assert result["status"] == "denied" and ACCOUNT not in json.dumps(result)
