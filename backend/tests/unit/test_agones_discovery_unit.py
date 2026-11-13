"""Unit tests for the bounded, read-only Agones inventory (#529).

The inventory reads through the guarded EKS MCP path. These tests drive the
real ``EksMcpGuardedProvider`` with fake delegates that reproduce the installed
eks-mcp-server wire shape (``[TextContent(prose), TextContent(json)]``) and
prove:

* the model can never reach ``manage_k8s_resource``, choose a kind, or send a
  body; every read is ``operation="read"`` on a pinned Agones kind;
* Secrets and every non-Agones kind are unreachable;
* authority comes only from the server-owned cluster scope;
* item, nested-list, string, nesting-depth, and serialized-size bounds hold;
* annotations, env values, endpoints, addresses, account IDs, registry hosts,
  and provider error text never reach the model or logs; and
* empty, denied, unavailable, malformed, partial, stale, and truncated stay
  distinguishable.

Synthetic, public-safe fixtures only.
"""

# Standard library
import asyncio
import json
from typing import Any, Callable, Sequence

# Third-party packages
import pytest
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
import agents.agones_discovery as ad
from agents.eks_mcp_guard import guard_eks_mcp_client

pytestmark = pytest.mark.unit

CLUSTER = "synthetic-games"
FAKE_ACCOUNT = "000000000000"
ECR_IMAGE = f"{FAKE_ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/game/echo-server:1.4.2"
SECRET_VALUE = "s3cr3t-synthetic-value"
ENDPOINT = "https://hooks.example.invalid/scale"
POD_IP = "10.20.30.40"
NODE = "ip-10-20-30-40.us-west-2.compute.internal"


# --------------------------------------------------------------------------- #
# Fake eks-mcp-server                                                         #
# --------------------------------------------------------------------------- #
def _success(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"text": "Success"}, {"text": json.dumps(payload)}]


class _FakeCluster:
    """Holds Agones objects per kind and answers list/read like eks-mcp-server."""

    def __init__(self) -> None:
        self.objects: dict[str, list[dict[str, Any]]] = {"Fleet": [], "FleetAutoscaler": [], "GameServer": []}
        self.list_error: dict[str, str] = {}
        self.read_error: dict[tuple[str, str], str] = {}
        self.list_override: dict[str, Any] = {}
        self.read_override: dict[tuple[str, str], Any] = {}
        self.list_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []
        self.hang = False
        self.api_versions: Any = ["apps/v1", "agones.dev/v1", "autoscaling.agones.dev/v1"]
        self.versions_error: str | None = None
        self.versions_calls: list[dict[str, Any]] = []

    def versions_result(self, inp: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        self.versions_calls.append(dict(inp))
        if self.versions_error is not None:
            return "error", [{"text": self.versions_error}]
        versions = self.api_versions
        count = len(versions) if isinstance(versions, list) else 0
        return "success", _success({"cluster_name": inp["cluster_name"], "api_versions": versions, "count": count})

    def list_result(self, inp: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        self.list_calls.append(dict(inp))
        kind = inp["kind"]
        if kind in self.list_error:
            return "error", [{"text": self.list_error[kind]}]
        if kind in self.list_override:
            return "success", _success(self.list_override[kind])
        items = [
            {
                "name": obj["metadata"]["name"],
                "namespace": obj["metadata"].get("namespace"),
                "labels": obj["metadata"].get("labels"),
                "annotations": obj["metadata"].get("annotations"),
            }
            for obj in self.objects.get(kind, [])
        ]
        return "success", _success(
            {
                "kind": kind,
                "api_version": inp["api_version"],
                "namespace": inp.get("namespace"),
                "count": len(items),
                "items": items,
            }
        )

    def read_result(self, inp: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        self.read_calls.append(dict(inp))
        key = (inp["kind"], inp["name"])
        if key in self.read_error:
            return "error", [{"text": self.read_error[key]}]
        if key in self.read_override:
            return "success", _success(self.read_override[key])
        for obj in self.objects.get(inp["kind"], []):
            if obj["metadata"]["name"] == inp["name"]:
                return "success", _success(
                    {
                        "kind": inp["kind"],
                        "name": inp["name"],
                        "namespace": inp.get("namespace"),
                        "api_version": inp["api_version"],
                        "operation": "read",
                        "resource": obj,
                    }
                )
        return "error", [{"text": f"Failed to read {inp['kind']}: (404) Not Found"}]


class _FakeTool(AgentTool):
    def __init__(
        self, name: str, handler: Callable[[dict[str, Any]], tuple[str, list[dict[str, Any]]]], cluster: _FakeCluster
    ):
        super().__init__()
        self._name = name
        self._handler = handler
        self._cluster = cluster

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": "orig", "inputSchema": {"json": {"type": "object"}}}

    @property
    def tool_type(self) -> str:
        return "mcp"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        if self._cluster.hang:
            await asyncio.sleep(3600)
        status, content = self._handler(dict(tool_use["input"]))
        yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], "status": status, "content": content})


class _Provider(ToolProvider):
    def __init__(self, tools: Sequence[AgentTool]) -> None:
        self.tools = tools

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        return self.tools

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass


def _tools(cluster: _FakeCluster) -> list[AgentTool]:
    delegates = [
        _FakeTool("list_k8s_resources", cluster.list_result, cluster),
        _FakeTool("manage_k8s_resource", cluster.read_result, cluster),
        _FakeTool("apply_yaml", cluster.read_result, cluster),
        _FakeTool("list_api_versions", cluster.versions_result, cluster),
    ]
    return list(asyncio.run(guard_eks_mcp_client("eks-mcp-server", _Provider(delegates)).load_tools()))


def _inventory_tool(cluster: _FakeCluster) -> AgentTool:
    tool = next(t for t in _tools(cluster) if t.tool_name == "get_agones_inventory")
    return tool


def _run(tool: AgentTool, tool_input: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    use: ToolUse = {"toolUseId": "tu-1", "name": tool.tool_name, "input": tool_input}

    async def _go() -> dict[str, Any]:
        final = None
        async for event in tool.stream(use, {}):
            if isinstance(event, ToolResultEvent):
                final = event.tool_result
        assert final is not None
        return final

    result = asyncio.run(_go())
    text = result["content"][0]["text"]
    return result["status"], json.loads(text), text


@pytest.fixture(autouse=True)
def _enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ad.settings, "EKS_DISCOVERY_CLUSTERS_RAW", CLUSTER, raising=False)
    monkeypatch.setattr(ad.settings, "AWS_REGION", "us-west-2", raising=False)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
def _fleet(name: str, namespace: str = "games", ports: int = 1, containers: int = 1) -> dict[str, Any]:
    return {
        "apiVersion": "agones.dev/v1",
        "kind": "Fleet",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": "2026-10-01T00:00:00Z",
            "labels": {"app": name, "team-secret": SECRET_VALUE},
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": json.dumps({"env": SECRET_VALUE})},
            "uid": "synthetic-uid",
        },
        "spec": {
            "replicas": 3,
            "scheduling": "Packed",
            "strategy": {"type": "RollingUpdate"},
            "template": {
                "spec": {
                    "ports": [
                        {"name": f"p{i}", "portPolicy": "Dynamic", "containerPort": 7654 + i, "protocol": "UDP"}
                        for i in range(ports)
                    ],
                    "health": {"initialDelaySeconds": 30, "periodSeconds": 10},
                    "template": {
                        "spec": {
                            "serviceAccountName": "synthetic-sa",
                            "nodeSelector": {"node": NODE},
                            "containers": [
                                {
                                    "name": f"server{i}",
                                    "image": ECR_IMAGE,
                                    "env": [{"name": "API_TOKEN", "value": SECRET_VALUE}],
                                    "envFrom": [{"secretRef": {"name": "game-secrets"}}],
                                    "command": ["/bin/sh", "-c", f"curl {ENDPOINT}"],
                                    "resources": {
                                        "requests": {"cpu": "20m", "memory": "64Mi"},
                                        "limits": {"cpu": "20m", "memory": "64Mi"},
                                    },
                                }
                                for i in range(containers)
                            ],
                        }
                    },
                }
            },
        },
        "status": {"replicas": 3, "readyReplicas": 2, "allocatedReplicas": 1, "reservedReplicas": 0},
    }


def _gameserver(name: str, fleet: str = "echo", state: str = "Ready") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": "games",
            "labels": {"agones.dev/fleet": fleet},
            "annotations": {"note": SECRET_VALUE},
        },
        "spec": {"ports": [{"name": "default", "portPolicy": "Dynamic", "containerPort": 7654, "protocol": "UDP"}]},
        "status": {
            "state": state,
            "address": POD_IP,
            "addresses": [{"type": "InternalIP", "address": POD_IP}],
            "nodeName": NODE,
            "ports": [{"name": "default", "port": 7123}],
        },
    }


def _autoscaler(name: str, policy: str = "Buffer") -> dict[str, Any]:
    spec: dict[str, Any] = {
        "fleetName": "echo",
        "policy": {"type": policy},
        "sync": {"type": "FixedInterval", "fixedInterval": {"seconds": 30}},
    }
    if policy == "Buffer":
        spec["policy"]["buffer"] = {"bufferSize": "20%", "minReplicas": 2, "maxReplicas": 10}
    else:
        spec["policy"]["webhook"] = {"url": ENDPOINT, "caBundle": "c3ludGhldGlj"}
    return {"metadata": {"name": name, "namespace": "games"}, "spec": spec}


def _populated() -> _FakeCluster:
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet("echo")]
    cluster.objects["FleetAutoscaler"] = [_autoscaler("echo-autoscaler")]
    cluster.objects["GameServer"] = [_gameserver("echo-a"), _gameserver("echo-b", state="Allocated")]
    return cluster


# --------------------------------------------------------------------------- #
# Read-only surface                                                           #
# --------------------------------------------------------------------------- #
def test_manage_k8s_resource_and_mutating_tools_are_not_model_facing():
    names = {t.tool_name for t in _tools(_populated())}
    assert "manage_k8s_resource" not in names
    assert "apply_yaml" not in names
    assert "get_agones_inventory" in names


def test_inventory_schema_has_no_operation_kind_body_or_selector():
    spec = _inventory_tool(_populated()).tool_spec
    schema = spec["inputSchema"]["json"]
    assert set(schema["properties"]) == {"cluster_name", "namespace"}
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["cluster_name"]


def test_every_delegate_read_is_pinned_read_on_an_agones_kind():
    cluster = _populated()
    _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert cluster.read_calls, "expected reads"
    for call in cluster.read_calls:
        assert call["operation"] == ad.READ_OPERATION == "read"
        assert (call["kind"], call["api_version"]) in ad.AGONES_READ_KINDS
        assert "body" not in call
        assert call["cluster_name"] == CLUSTER
    for call in cluster.list_calls:
        assert (call["kind"], call["api_version"]) in ad.AGONES_READ_KINDS
        assert set(call) <= {"cluster_name", "kind", "api_version", "namespace"}


def test_secret_kind_is_unreachable():
    assert all(kind.lower() not in ("secret", "secrets") for kind, _ in ad.AGONES_READ_KINDS)
    assert {kind for kind, _ in ad.AGONES_READ_KINDS} == {"Fleet", "FleetAutoscaler", "GameServer"}


@pytest.mark.parametrize(
    "tool_input",
    [
        {"cluster_name": CLUSTER, "kind": "Secret"},
        {"cluster_name": CLUSTER, "operation": "delete"},
        {"cluster_name": CLUSTER, "body": {"spec": {}}},
        {"cluster_name": CLUSTER, "api_version": "v1"},
        {"cluster_name": f"arn:aws:eks:us-west-2:{FAKE_ACCOUNT}:cluster/x"},
        {"cluster_name": FAKE_ACCOUNT},
        {"cluster_name": CLUSTER, "namespace": "Bad_NS"},
        {},
    ],
)
def test_out_of_contract_input_is_denied_before_any_provider_call(tool_input):
    cluster = _populated()
    status, body, _ = _run(_inventory_tool(cluster), tool_input)
    assert status == "error"
    assert body["outcome"] == "denied"
    assert body["errorCode"] == "denied_input"
    assert cluster.list_calls == [] and cluster.read_calls == []


# --------------------------------------------------------------------------- #
# Server-owned authority                                                      #
# --------------------------------------------------------------------------- #
def test_cluster_outside_server_owned_scope_fails_closed(monkeypatch):
    cluster = _populated()
    status, body, _ = _run(_inventory_tool(cluster), {"cluster_name": "other-cluster"})
    assert status == "error" and body["errorCode"] == "not_enrolled"
    assert cluster.list_calls == []


def test_unset_scope_fails_closed(monkeypatch):
    monkeypatch.setattr(ad.settings, "EKS_DISCOVERY_CLUSTERS_RAW", "", raising=False)
    cluster = _populated()
    status, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert status == "error" and body["errorCode"] == "not_enrolled"
    assert cluster.list_calls == []


def test_scope_parser_rejects_invalid_and_caps_entries():
    raw = ",".join([f"c{i}" for i in range(50)] + [FAKE_ACCOUNT, "bad name", "arn:aws:eks:x"])
    scope = ad.enrolled_clusters(raw)
    assert len(scope) == ad.MAX_ENROLLED_CLUSTERS
    assert FAKE_ACCOUNT not in scope and "bad name" not in scope


def test_region_comes_from_runtime_setting_not_input():
    status, body, _ = _run(_inventory_tool(_populated()), {"cluster_name": CLUSTER})
    assert status == "success"
    assert body["scope"] == {"clusterName": CLUSTER, "region": "us-west-2"}
    assert body["readOnly"] is True


# --------------------------------------------------------------------------- #
# Projection content                                                          #
# --------------------------------------------------------------------------- #
def test_complete_inventory_projects_reviewed_fields_only():
    status, body, text = _run(_inventory_tool(_populated()), {"cluster_name": CLUSTER})
    assert status == "success"
    assert body["v"] == ad.AGONES_ENVELOPE_VERSION
    assert body["outcome"] == "complete"

    fleet = body["fleets"]["items"][0]
    assert fleet["name"] == "echo" and fleet["replicas"] == 3 and fleet["scheduling"] == "Packed"
    assert fleet["status"] == {"replicas": 3, "readyReplicas": 2, "allocatedReplicas": 1, "reservedReplicas": 0}
    assert fleet["labels"] == {"app": "echo"}
    port = fleet["template"]["ports"][0]
    assert port == {"name": "p0", "portPolicy": "Dynamic", "containerPort": 7654, "protocol": "UDP"}
    container = fleet["template"]["containers"][0]
    assert container["image"] == {
        "registryType": "ecr-private",
        "registryRegion": "us-west-2",
        "repository": "game/echo-server",
        "tag": "1.4.2",
        "complete": True,
    }
    assert container["resources"] == {
        "cpuRequest": "20m",
        "memoryRequest": "64Mi",
        "cpuLimit": "20m",
        "memoryLimit": "64Mi",
    }

    scaler = body["autoscalers"]["items"][0]
    assert scaler["policyType"] == "Buffer"
    assert scaler["buffer"] == {"bufferSize": "20%", "minReplicas": 2, "maxReplicas": 10}
    assert scaler["sync"] == {"type": "FixedInterval", "fixedIntervalSeconds": 30}

    gs = body["gameServers"]
    assert gs["stateCounts"] == {"Allocated": 1, "Ready": 1}
    assert gs["items"][0]["fleet"] == "echo"
    assert gs["items"][0]["statusPorts"] == [{"name": "default", "port": 7123}]


@pytest.mark.parametrize(
    "forbidden",
    [
        SECRET_VALUE,
        FAKE_ACCOUNT,
        "amazonaws.com",
        ENDPOINT,
        "hooks.example.invalid",
        POD_IP,
        NODE,
        "game-secrets",
        "API_TOKEN",
        "annotations",
        "last-applied",
        "serviceAccountName",
        "synthetic-sa",
        "caBundle",
        "uid",
        "/bin/sh",
    ],
)
def test_sensitive_and_unreviewed_values_never_reach_model(forbidden):
    cluster = _populated()
    cluster.objects["FleetAutoscaler"].append(_autoscaler("hook-scaler", policy="Webhook"))
    _, _, text = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert forbidden not in text


def test_webhook_policy_keeps_type_only():
    cluster = _populated()
    cluster.objects["FleetAutoscaler"] = [_autoscaler("hook-scaler", policy="Webhook")]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    scaler = body["autoscalers"]["items"][0]
    assert scaler["policyType"] == "Webhook" and scaler["detailsOmitted"] is True
    assert "buffer" not in scaler


def test_provider_error_text_never_reaches_model_or_logs(caplog):
    cluster = _populated()
    leak = f"forbidden: User arn:aws:sts::{FAKE_ACCOUNT}:assumed-role/x cannot list {ENDPOINT}"
    cluster.list_error["Fleet"] = leak
    # Local modules
    from utils.logger import logger

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        _, body, text = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    finally:
        logger.remove(sink)
    assert body["fleets"]["outcome"] == "denied"
    assert FAKE_ACCOUNT not in text and ENDPOINT not in text
    joined = "\n".join(messages)
    assert FAKE_ACCOUNT not in joined and ENDPOINT not in joined and "assumed-role" not in joined


# --------------------------------------------------------------------------- #
# Bounds                                                                      #
# --------------------------------------------------------------------------- #
def test_item_caps_per_section_and_has_more():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet(f"f{i:02d}") for i in range(ad.MAX_FLEETS + 5)]
    cluster.objects["GameServer"] = [_gameserver(f"g{i:02d}") for i in range(ad.MAX_GAMESERVERS + 7)]
    cluster.objects["FleetAutoscaler"] = [_autoscaler(f"a{i:02d}") for i in range(ad.MAX_AUTOSCALERS + 1)]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert len(body["fleets"]["items"]) == ad.MAX_FLEETS
    assert len(body["gameServers"]["items"]) == ad.MAX_GAMESERVERS
    assert len(body["autoscalers"]["items"]) == ad.MAX_AUTOSCALERS
    for key in ("fleets", "gameServers", "autoscalers"):
        assert body[key]["outcome"] == "truncated" and body[key]["hasMore"] is True
    assert body["gameServers"]["sampled"] is True
    assert body["outcome"] == "truncated" and body["truncated"] is True
    reads = [c for c in cluster.read_calls if c["kind"] == "Fleet"]
    assert len(reads) == ad.MAX_FLEETS
    # Deterministic order: the first N names sorted.
    assert [i["name"] for i in body["fleets"]["items"]] == [f"f{i:02d}" for i in range(ad.MAX_FLEETS)]


def test_nested_port_and_container_caps():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet("echo", ports=ad.MAX_PORTS + 3, containers=ad.MAX_CONTAINERS + 2)]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    template = body["fleets"]["items"][0]["template"]
    assert len(template["ports"]) == ad.MAX_PORTS
    assert len(template["containers"]) == ad.MAX_CONTAINERS
    assert body["fleets"]["nestedTruncated"] is True
    assert body["fleets"]["outcome"] == "truncated"
    assert "nestedDropped" not in body["fleets"]


def _fleet_with(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    fleet = _fleet("echo")
    mutate(fleet)
    return fleet


def _template(fleet: dict[str, Any]) -> dict[str, Any]:
    return fleet["spec"]["template"]["spec"]


def _container(fleet: dict[str, Any]) -> dict[str, Any]:
    return _template(fleet)["template"]["spec"]["containers"][0]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda f: _template(f)["ports"][0].update(portPolicy="Bogus"), id="invalid-port-policy"),
        pytest.param(lambda f: _template(f)["ports"][0].update(containerPort=70_000), id="invalid-container-port"),
        pytest.param(lambda f: _template(f)["ports"].append("not-a-mapping"), id="non-mapping-port"),
        pytest.param(lambda f: _template(f)["ports"].append({"protocol": "ICMP"}), id="all-invalid-port"),
        pytest.param(lambda f: _template(f).update(ports="7654"), id="ports-not-a-list"),
        pytest.param(lambda f: _container(f).update(name="Bad_Name"), id="invalid-container-name"),
        pytest.param(lambda f: _container(f)["resources"]["limits"].update(cpu="lots"), id="invalid-quantity"),
        pytest.param(lambda f: _template(f)["template"]["spec"]["containers"].append(7), id="non-mapping-container"),
    ],
)
def test_nested_drop_makes_the_section_partial_not_complete(mutate):
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet_with(mutate)]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    fleets = body["fleets"]
    assert fleets["outcome"] == "partial" and fleets["partial"] is True
    assert fleets["nestedDropped"] >= 1
    assert body["outcome"] == "partial" and body["partial"] is True


def test_nested_drop_wins_over_a_nested_cap():
    cluster = _FakeCluster()
    fleet = _fleet("echo", ports=ad.MAX_PORTS + 1)
    _template(fleet)["ports"][0]["portPolicy"] = "Bogus"
    cluster.objects["Fleet"] = [fleet]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["fleets"]["outcome"] == "partial"
    assert body["fleets"]["nestedTruncated"] is True and body["fleets"]["nestedDropped"] >= 1


def test_invalid_gameserver_status_port_makes_the_section_partial():
    cluster = _FakeCluster()
    server = _gameserver("echo-a")
    server["status"]["ports"].append({"name": "extra", "port": 0})
    cluster.objects["GameServer"] = [server]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["gameServers"]["outcome"] == "partial"
    assert body["gameServers"]["nestedDropped"] == 1


def test_clean_inventory_has_no_nested_markers():
    _, body, _ = _run(_inventory_tool(_populated()), {"cluster_name": CLUSTER})
    assert body["outcome"] == "complete"
    for section in ("fleets", "autoscalers", "gameServers"):
        assert "nestedDropped" not in body[section] and "nestedTruncated" not in body[section]


def test_string_cap_and_grammar_drop_oversized_values():
    fleet = _fleet("echo")
    fleet["spec"]["template"]["spec"]["ports"][0]["name"] = "p" * (ad.MAX_STRING + 1)
    fleet["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["name"] = "x" * 200
    fleet["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "9" * 40
    projected = ad.project_fleet(fleet)
    assert projected is not None
    assert "name" not in projected["template"]["ports"][0]
    container = projected["template"]["containers"][0]
    assert "name" not in container and "cpuRequest" not in container["resources"]
    long_name = _fleet("a" * (ad.MAX_STRING + 1))
    assert ad.project_fleet(long_name) is None


def test_every_string_in_maximal_envelope_respects_string_cap():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [
        _fleet(f"f{i:02d}", ports=ad.MAX_PORTS, containers=ad.MAX_CONTAINERS) for i in range(ad.MAX_FLEETS)
    ]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})

    def strings(value):
        if isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from strings(v)
        elif isinstance(value, list):
            for v in value:
                yield from strings(v)
        elif isinstance(value, str):
            yield value

    assert all(len(s) <= ad.MAX_STRING for s in strings(body) if s != body.get("message"))


def test_nesting_depth_bound_holds_for_maximal_envelope():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet("echo", ports=ad.MAX_PORTS, containers=ad.MAX_CONTAINERS)]
    cluster.objects["GameServer"] = [_gameserver("g1")]
    cluster.objects["FleetAutoscaler"] = [_autoscaler("a1")]
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert ad.nesting_depth(body) <= ad.MAX_DEPTH


def test_depth_overflow_is_refused_as_malformed():
    deep: dict = {"v": ad.AGONES_ENVELOPE_VERSION}
    cursor = deep
    for _ in range(ad.MAX_DEPTH + 2):
        cursor["x"] = {}
        cursor = cursor["x"]
    cursor["leaf"] = 1
    out = ad.finalize_envelope(deep)
    assert out["outcome"] == "malformed"


def test_serialized_size_cap_trims_then_withholds(monkeypatch):
    monkeypatch.setattr(ad, "MAX_ENVELOPE_BYTES", 2_500)
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet(f"f{i:02d}", ports=ad.MAX_PORTS) for i in range(ad.MAX_FLEETS)]
    _, body, text = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert len(text.encode("utf-8")) <= 2_500
    assert body["truncated"] is True
    if "fleets" in body:
        assert body["fleets"]["truncated"] is True and body["fleets"]["returned"] < ad.MAX_FLEETS

    monkeypatch.setattr(ad, "MAX_ENVELOPE_BYTES", 300)
    _, body, text = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert len(text.encode("utf-8")) <= 300
    assert body["outcome"] == "truncated" and "fleets" not in body


def test_default_size_cap_holds_for_maximal_inventory():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [
        _fleet(f"f{i:02d}", ports=ad.MAX_PORTS, containers=ad.MAX_CONTAINERS) for i in range(ad.MAX_FLEETS)
    ]
    cluster.objects["GameServer"] = [_gameserver(f"g{i:02d}") for i in range(ad.MAX_GAMESERVERS)]
    cluster.objects["FleetAutoscaler"] = [_autoscaler(f"a{i:02d}") for i in range(ad.MAX_AUTOSCALERS)]
    _, _, text = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert len(text.encode("utf-8")) <= ad.MAX_ENVELOPE_BYTES


# --------------------------------------------------------------------------- #
# Distinguishable outcomes                                                    #
# --------------------------------------------------------------------------- #
def test_empty():
    _, body, _ = _run(_inventory_tool(_FakeCluster()), {"cluster_name": CLUSTER})
    assert body["outcome"] == "empty"
    assert all(body[k]["outcome"] == "empty" for k in ("fleets", "autoscalers", "gameServers"))


def test_denied_when_rbac_refuses_agones_group():
    cluster = _populated()
    for kind in ("Fleet", "FleetAutoscaler", "GameServer"):
        cluster.list_error[kind] = "fleets.agones.dev is forbidden: cannot list resource"
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["outcome"] == "denied"


def test_unavailable_on_provider_failure():
    cluster = _populated()
    for kind in ("Fleet", "FleetAutoscaler", "GameServer"):
        cluster.list_error[kind] = "connection reset by peer"
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["outcome"] == "unavailable"


def test_unavailable_on_deadline(monkeypatch):
    monkeypatch.setattr(ad, "INVENTORY_DEADLINE_SECONDS", 0.05)
    cluster = _populated()
    cluster.hang = True
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["outcome"] == "unavailable"


def test_malformed_list_shape():
    cluster = _populated()
    for kind in ("Fleet", "FleetAutoscaler", "GameServer"):
        cluster.list_override[kind] = {"kind": kind, "api_version": "wrong/v9", "count": 1, "items": []}
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["outcome"] == "malformed"


def test_malformed_read_payload():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet("echo")]
    cluster.read_override[("Fleet", "echo")] = {"kind": "Fleet", "resource": "not-a-mapping"}
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["fleets"]["outcome"] == "malformed"
    assert body["fleets"]["failures"] == {"malformed": 1}


def test_partial_when_some_reads_fail():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet("echo"), _fleet("other")]
    cluster.read_error[("Fleet", "other")] = "connection reset"
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["fleets"]["outcome"] == "partial" and body["fleets"]["partial"] is True
    assert body["fleets"]["returned"] == 1
    assert body["outcome"] == "partial"


def test_partial_when_list_count_disagrees():
    cluster = _FakeCluster()
    cluster.list_override["Fleet"] = {"kind": "Fleet", "api_version": "agones.dev/v1", "count": 5, "items": []}
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["fleets"]["outcome"] == "partial"


def test_stale_when_object_disappears_between_list_and_read():
    cluster = _FakeCluster()
    cluster.objects["Fleet"] = [_fleet("echo"), _fleet("gone")]
    cluster.read_error[("Fleet", "gone")] = "Failed to read Fleet games/gone: (404) Reason: Not Found"
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["fleets"]["stale"] is True and body["stale"] is True
    assert body["fleets"]["failures"] == {"stale": 1}
    assert body["fleets"]["outcome"] == "partial"


def test_outcomes_are_mutually_distinct():
    outcomes = {"complete", "empty", "denied", "unavailable", "malformed", "partial", "truncated"}
    assert len(outcomes) == 7
    assert ad.combine_outcomes(["complete", "empty", "empty"]) == "complete"
    assert ad.combine_outcomes(["complete", "truncated"]) == "truncated"
    assert ad.combine_outcomes(["complete", "denied"]) == "partial"
    assert ad.combine_outcomes(["denied", "denied", "denied"]) == "denied"


# --------------------------------------------------------------------------- #
# Agones not installed (#781 review item 5)                                   #
# --------------------------------------------------------------------------- #
def test_agones_not_installed_is_distinct_and_reads_nothing():
    cluster = _populated()
    cluster.api_versions = ["apps/v1", "batch/v1", "networking.k8s.io/v1"]
    status, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert status == "error"
    assert body["outcome"] == ad.OUTCOME_NOT_INSTALLED == "not_installed"
    assert body["errorCode"] == "AGONES_NOT_INSTALLED"
    assert cluster.versions_calls == [{"cluster_name": CLUSTER}]
    assert cluster.list_calls == [] and cluster.read_calls == []


def test_not_installed_differs_from_unavailable():
    cluster = _populated()
    cluster.list_error = {kind: "Failed to list Fleet resources: timed out" for kind in cluster.objects}
    _, unavailable, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert unavailable["outcome"] == "unavailable" and "errorCode" not in unavailable

    cluster.api_versions = ["apps/v1"]
    _, not_installed, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert not_installed["outcome"] == "not_installed"


def test_agones_installed_reads_as_usual():
    cluster = _populated()
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["outcome"] == "complete"
    assert cluster.versions_calls == [{"cluster_name": CLUSTER}]


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda c: setattr(c, "versions_error", "Failed to get API versions: (403) forbidden"), id="error"),
        pytest.param(lambda c: setattr(c, "api_versions", "agones.dev/v1"), id="not-a-list"),
        pytest.param(lambda c: setattr(c, "api_versions", []), id="empty-list"),
    ],
)
def test_undecided_version_check_falls_back_to_the_reads(setup):
    cluster = _populated()
    setup(cluster)
    _, body, _ = _run(_inventory_tool(cluster), {"cluster_name": CLUSTER})
    assert body["outcome"] == "complete"
    assert cluster.list_calls


def test_version_check_is_not_made_for_an_unenrolled_cluster():
    cluster = _populated()
    _run(_inventory_tool(cluster), {"cluster_name": "other-cluster"})
    assert cluster.versions_calls == []
