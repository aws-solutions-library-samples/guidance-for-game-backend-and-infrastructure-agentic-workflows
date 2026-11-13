"""Bounded, read-only Agones inventory for the EKS specialist (#529).

The migration advisor needs a small, reviewed view of an Agones workload:
Fleets, GameServers, FleetAutoscalers, and the allocation, scaling, image,
port, and Region facts they carry. This module adds ONE code-owned model tool,
``get_agones_inventory``, that reads them through the existing read-only EKS
MCP path and projects each object into a reviewed schema.

Trust and authority
-------------------
* The model supplies only ``cluster_name`` and an optional ``namespace``. The
  cluster must appear in the server-owned enrollment scope
  (``GBAW_EKS_DISCOVERY_CLUSTERS``); otherwise the tool fails closed before any
  provider call. A tool argument can narrow the scope but never widen it.
* Kubernetes authorization is unchanged: the read uses the runtime role's
  existing EKS access entry and cluster RBAC. Enrollment grants no write verb
  and no Secret access. A cluster without read access to the Agones API groups
  returns a typed ``denied`` outcome; this tool never broadens RBAC.
* Region is the runtime's own ``AWS_REGION`` setting, not a model value.

Read-only by construction
-------------------------
* The model-facing schema has no ``operation``, ``kind``, ``api_version``,
  ``body``, or selector field. Those values are fixed in code.
* ``list_api_versions`` is called first, with only the cluster name. When the
  cluster answers and does not serve ``agones.dev/v1``, the tool returns a
  distinct ``not_installed`` outcome (``AGONES_NOT_INSTALLED``) instead of
  reading kinds that do not exist. If that check fails, the reads still run and
  report their own typed outcomes.
* ``list_k8s_resources`` is called with a pinned kind and API version.
  ``manage_k8s_resource`` is called ONLY with ``operation="read"``, a pinned
  Agones kind, a name taken from the list result, and no ``body``. That
  delegate stays off the model-facing allowlist in ``eks_mcp_guard``.
* The read set is the frozen ``AGONES_READ_KINDS``; ``Secret`` and every other
  kind are unreachable. The embedded EKS MCP server also runs without
  ``--allow-write`` and ``--allow-sensitive-data-access``.

Bounds
------
Item caps per collection (``MAX_FLEETS``, ``MAX_AUTOSCALERS``,
``MAX_GAMESERVERS``), nested-list caps (``MAX_PORTS``, ``MAX_CONTAINERS``), a
per-string cap (``MAX_STRING``), a nesting-depth cap (``MAX_DEPTH``), a
serialized UTF-8 byte cap (``MAX_ENVELOPE_BYTES``), and an overall time budget
(``INVENTORY_DEADLINE_SECONDS``). Every value passes a field grammar, enum, or
numeric range. Annotations, environment values, commands, arguments, volumes,
service accounts, node names, pod addresses, webhook endpoints, image
registry hosts, and every unreviewed field are dropped.

Outcomes
--------
Each section and the envelope carry one of ``complete``, ``empty``,
``denied``, ``unavailable``, ``malformed``, ``partial``, or ``truncated``
(the whole envelope can also be ``not_installed``),
plus independent ``stale``, ``partial``, ``truncated``, and ``hasMore`` flags
and code-owned failure counts. Provider text never reaches the model or logs.

A mixed section is never ``complete`` (the #464 rule). A nested list cut at
its cap makes a complete section ``truncated`` with ``nestedTruncated``. A
nested port or container entry dropped because it failed validation makes the
section ``partial`` with ``nestedDropped`` (this module's equivalent of
GameLift's ``incomplete`` + ``malformed_response``).
"""

from __future__ import annotations

# Standard library
import asyncio
import re
from typing import Any, Mapping, Sequence

# Third-party packages
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
from agents.eks_mcp_guard import (
    _CLASSIFY_ERROR_PREFIX,
    _CONTROL_CHARS,
    _EKS_NAME,
    _ISO_TIMESTAMP,
    _NAMESPACE,
    _SENSITIVE_SCALAR_SHAPES,
    AGONES_INVENTORY_TOOL,
    OUTCOME_COMPLETE,
    OUTCOME_DENIED,
    OUTCOME_EMPTY,
    OUTCOME_MALFORMED,
    OUTCOME_PARTIAL,
    OUTCOME_TRUNCATED,
    OUTCOME_UNAVAILABLE,
    _drain_final_result,
    _dumps,
    _extract_eks_payload,
    _log_outcome,
    _reviewed_labels,
)
from agents.image_reference import project_image_reference
from config import settings

AGONES_ENVELOPE_VERSION = "agones-discovery-1"

# --------------------------------------------------------------------------- #
# Bounds                                                                      #
# --------------------------------------------------------------------------- #
MAX_FLEETS = 10
MAX_AUTOSCALERS = 10
MAX_GAMESERVERS = 20
MAX_PORTS = 8
MAX_CONTAINERS = 4
MAX_STRING = 128
MAX_DEPTH = 8
MAX_ENVELOPE_BYTES = 20_000
MAX_LISTED_NAMES = 500  # names considered from one list result before sorting
MAX_ENROLLED_CLUSTERS = 20
INVENTORY_DEADLINE_SECONDS = 45.0

_MAX_PORT = 65_535
_MAX_REPLICAS = 100_000
_MAX_SECONDS = 86_400

# --------------------------------------------------------------------------- #
# Fixed read plan. Section key -> (kind, apiVersion, item cap).               #
# --------------------------------------------------------------------------- #
_SECTION_FLEETS = "fleets"
_SECTION_AUTOSCALERS = "autoscalers"
_SECTION_GAMESERVERS = "gameServers"

_READ_PLAN: tuple[tuple[str, str, str, int], ...] = (
    (_SECTION_FLEETS, "Fleet", "agones.dev/v1", MAX_FLEETS),
    (_SECTION_AUTOSCALERS, "FleetAutoscaler", "autoscaling.agones.dev/v1", MAX_AUTOSCALERS),
    (_SECTION_GAMESERVERS, "GameServer", "agones.dev/v1", MAX_GAMESERVERS),
)
AGONES_READ_KINDS = frozenset((kind, api_version) for _, kind, api_version, _ in _READ_PLAN)
READ_OPERATION = "read"
AGONES_API_VERSION = "agones.dev/v1"
MAX_API_VERSIONS = 2_000  # entries considered from one list_api_versions result

# --------------------------------------------------------------------------- #
# Fixed messages and codes                                                    #
# --------------------------------------------------------------------------- #
_MSG_NOT_ENROLLED = "This cluster is not in the assistant's enrolled discovery scope."
_MSG_BAD_INPUT = "This request was rejected because an argument is not permitted or out of bounds."
_MSG_WITHHELD = "The Agones inventory exceeded the maximum size and was withheld."
_MSG_NOT_INSTALLED = "Agones is not installed on this cluster: it does not serve the agones.dev/v1 API."

_CODE_NOT_ENROLLED = "not_enrolled"
_CODE_DENIED_INPUT = "denied_input"
_CODE_NOT_INSTALLED = "AGONES_NOT_INSTALLED"
OUTCOME_NOT_INSTALLED = "not_installed"

# --------------------------------------------------------------------------- #
# Grammars and enums                                                          #
# --------------------------------------------------------------------------- #
_K8S_OBJECT_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,126}[a-z0-9])?$")
_PORT_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$")
_CONTAINER_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_QUANTITY = re.compile(r"^\d{1,9}(\.\d{1,6})?(m|k|Ki|Mi|Gi|Ti|Pi|Ei|M|G|T|P|E)?$")
_PERCENT = re.compile(r"^(100|[1-9]?[0-9])%$")
_REGION = re.compile(r"^[a-z]{2,}(-[a-z0-9]+){1,4}$")

_PORT_POLICIES = frozenset({"Dynamic", "Static", "Passthrough", "None"})
_PROTOCOLS = frozenset({"UDP", "TCP", "TCPUDP"})
_SCHEDULING = frozenset({"Packed", "Distributed"})
_STRATEGIES = frozenset({"RollingUpdate", "Recreate"})
_GAMESERVER_STATES = frozenset(
    {
        "PortAllocation",
        "Creating",
        "Starting",
        "Scheduled",
        "RequestReady",
        "Ready",
        "Shutdown",
        "Error",
        "Unhealthy",
        "Reserved",
        "Allocated",
    }
)
_POLICY_TYPES = frozenset({"Buffer", "Webhook", "Counter", "List", "Schedule", "Chain", "Wasm"})
_SYNC_TYPES = frozenset({"FixedInterval"})
_FLEET_LABEL = "agones.dev/fleet"


# --------------------------------------------------------------------------- #
# Server-owned scope                                                          #
# --------------------------------------------------------------------------- #
def enrolled_clusters(raw: str | None = None) -> frozenset[str]:
    """Parse the server-owned cluster scope.

    Reads ``settings.EKS_DISCOVERY_CLUSTERS_RAW`` unless ``raw`` is given. Each
    entry must match the EKS cluster-name grammar and carry no sensitive
    shape; invalid entries are ignored. At most ``MAX_ENROLLED_CLUSTERS``
    entries are accepted, so a malformed setting cannot widen the scope.
    """
    value = settings.EKS_DISCOVERY_CLUSTERS_RAW if raw is None else raw
    if not isinstance(value, str):
        return frozenset()
    names: list[str] = []
    for part in value.split(","):
        name = part.strip()
        if not name or not _EKS_NAME.match(name):
            continue
        if any(shape.search(name) for shape in _SENSITIVE_SCALAR_SHAPES):
            continue
        if name not in names:
            names.append(name)
        if len(names) >= MAX_ENROLLED_CLUSTERS:
            break
    return frozenset(names)


def _runtime_region() -> str | None:
    region = settings.AWS_REGION
    return region if isinstance(region, str) and _REGION.match(region) else None


# --------------------------------------------------------------------------- #
# Scalar helpers                                                              #
# --------------------------------------------------------------------------- #
def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string(value: Any, grammar: "re.Pattern[str]", limit: int = MAX_STRING) -> str | None:
    """A grammar-valid string with no control character or sensitive shape."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if _CONTROL_CHARS.search(value) or not grammar.match(value):
        return None
    if any(shape.search(value) for shape in _SENSITIVE_SCALAR_SHAPES):
        return None
    return value


def _enum(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _int(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40 or not _ISO_TIMESTAMP.match(value):
        return None
    return value


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop absent values so the projection carries only validated facts."""
    return {key: value for key, value in values.items() if value is not None and value != {} and value != []}


def _metadata(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project reviewed metadata. Annotations are never read."""
    meta = _mapping(obj.get("metadata"))
    name = _string(meta.get("name"), _K8S_OBJECT_NAME)
    if name is None:
        return None
    return _compact(
        {
            "name": name,
            "namespace": _string(meta.get("namespace"), _NAMESPACE),
            "creationTimestamp": _timestamp(meta.get("creationTimestamp")),
            "labels": _reviewed_labels(meta.get("labels")),
        }
    )


# --------------------------------------------------------------------------- #
# Nested projections                                                          #
# --------------------------------------------------------------------------- #
class _Flags:
    """Collects per-item bound hits and validation drops for one section."""

    def __init__(self) -> None:
        self.nested_truncated = False
        self.nested_dropped = 0


def _checked(raw: Mapping[str, Any], values: Mapping[str, Any], flags: _Flags) -> dict[str, Any]:
    """Compact ``values`` and record a drop when a present raw field failed validation.

    ``values`` maps each reviewed raw key to its validated value (``None`` when
    it failed). A raw value that is present but failed counts as one dropped
    nested entry, so the section cannot report ``complete``.
    """
    if any(raw.get(key) is not None and value is None for key, value in values.items()):
        flags.nested_dropped += 1
    return _compact(values)


def _nested_list(value: Any, cap: int, flags: _Flags) -> list[Any]:
    """The capped raw list. A non-list present value counts as a drop."""
    if not isinstance(value, list):
        if value is not None:
            flags.nested_dropped += 1
        return []
    if len(value) > cap:
        flags.nested_truncated = True
    return value[:cap]


def _project_ports(value: Any, flags: _Flags) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    for raw in _nested_list(value, MAX_PORTS, flags):
        if not isinstance(raw, Mapping):
            flags.nested_dropped += 1
            continue
        projected = _checked(
            raw,
            {
                "name": _string(raw.get("name"), _PORT_NAME),
                "portPolicy": _enum(raw.get("portPolicy"), _PORT_POLICIES),
                "protocol": _enum(raw.get("protocol"), _PROTOCOLS),
                "containerPort": _int(raw.get("containerPort"), 1, _MAX_PORT),
                "hostPort": _int(raw.get("hostPort"), 1, _MAX_PORT),
                "container": _string(raw.get("container"), _CONTAINER_NAME),
            },
            flags,
        )
        if projected:
            ports.append(projected)
        else:
            flags.nested_dropped += 1
    return ports


def _project_resources(value: Any, flags: _Flags) -> dict[str, Any]:
    resources = _mapping(value)
    requests = _mapping(resources.get("requests"))
    limits = _mapping(resources.get("limits"))
    projected = _compact(
        {
            "cpuRequest": _string(requests.get("cpu"), _QUANTITY, 32),
            "memoryRequest": _string(requests.get("memory"), _QUANTITY, 32),
            "cpuLimit": _string(limits.get("cpu"), _QUANTITY, 32),
            "memoryLimit": _string(limits.get("memory"), _QUANTITY, 32),
        }
    )
    raw = {
        "cpuRequest": requests.get("cpu"),
        "memoryRequest": requests.get("memory"),
        "cpuLimit": limits.get("cpu"),
        "memoryLimit": limits.get("memory"),
    }
    if any(raw_value is not None and key not in projected for key, raw_value in raw.items()):
        flags.nested_dropped += 1
    return projected


def _project_containers(pod_spec: Mapping[str, Any], flags: _Flags) -> list[dict[str, Any]]:
    """Containers keep name, image parts, and CPU/memory only.

    ``env``, ``envFrom``, ``command``, ``args``, ``volumeMounts``, and every
    other container field are dropped.
    """
    containers: list[dict[str, Any]] = []
    for raw in _nested_list(pod_spec.get("containers"), MAX_CONTAINERS, flags):
        if not isinstance(raw, Mapping):
            flags.nested_dropped += 1
            continue
        projected = _checked(
            raw,
            {
                "name": _string(raw.get("name"), _CONTAINER_NAME),
                "image": project_image_reference(raw.get("image")),
                "resources": _project_resources(raw.get("resources"), flags),
            },
            flags,
        )
        if projected:
            containers.append(projected)
        else:
            flags.nested_dropped += 1
    return containers


def _project_health(value: Any) -> dict[str, Any]:
    health = _mapping(value)
    return _compact(
        {
            "disabled": _bool(health.get("disabled")),
            "initialDelaySeconds": _int(health.get("initialDelaySeconds"), 0, _MAX_SECONDS),
            "periodSeconds": _int(health.get("periodSeconds"), 0, _MAX_SECONDS),
            "failureThreshold": _int(health.get("failureThreshold"), 0, 1_000),
        }
    )


def _project_gameserver_spec(spec: Mapping[str, Any], flags: _Flags) -> dict[str, Any]:
    pod_spec = _mapping(_mapping(spec.get("template")).get("spec"))
    return _compact(
        {
            "ports": _project_ports(spec.get("ports"), flags),
            "container": _string(spec.get("container"), _CONTAINER_NAME),
            "health": _project_health(spec.get("health")),
            "scheduling": _enum(spec.get("scheduling"), _SCHEDULING),
            "containers": _project_containers(pod_spec, flags),
        }
    )


# --------------------------------------------------------------------------- #
# Object projections                                                          #
# --------------------------------------------------------------------------- #
def project_fleet(obj: Any, flags: _Flags | None = None) -> dict[str, Any] | None:
    """Project one Agones Fleet. Returns ``None`` when it has no valid name."""
    flags = flags or _Flags()
    resource = _mapping(obj)
    meta = _metadata(resource)
    if meta is None:
        return None
    spec = _mapping(resource.get("spec"))
    status = _mapping(resource.get("status"))
    return _compact(
        {
            **meta,
            "replicas": _int(spec.get("replicas"), 0, _MAX_REPLICAS),
            "scheduling": _enum(spec.get("scheduling"), _SCHEDULING),
            "strategy": _enum(_mapping(spec.get("strategy")).get("type"), _STRATEGIES),
            "template": _project_gameserver_spec(_mapping(_mapping(spec.get("template")).get("spec")), flags),
            "status": _compact(
                {
                    "replicas": _int(status.get("replicas"), 0, _MAX_REPLICAS),
                    "readyReplicas": _int(status.get("readyReplicas"), 0, _MAX_REPLICAS),
                    "allocatedReplicas": _int(status.get("allocatedReplicas"), 0, _MAX_REPLICAS),
                    "reservedReplicas": _int(status.get("reservedReplicas"), 0, _MAX_REPLICAS),
                }
            ),
        }
    )


def project_gameserver(obj: Any, flags: _Flags | None = None) -> dict[str, Any] | None:
    """Project one GameServer. Address, addresses, and node name are dropped."""
    flags = flags or _Flags()
    resource = _mapping(obj)
    meta = _metadata(resource)
    if meta is None:
        return None
    raw_meta = _mapping(resource.get("metadata"))
    spec = _mapping(resource.get("spec"))
    status = _mapping(resource.get("status"))
    status_ports: list[dict[str, Any]] = []
    for raw in _nested_list(status.get("ports"), MAX_PORTS, flags):
        if not isinstance(raw, Mapping):
            flags.nested_dropped += 1
            continue
        projected_port = _checked(
            raw,
            {"name": _string(raw.get("name"), _PORT_NAME), "port": _int(raw.get("port"), 1, _MAX_PORT)},
            flags,
        )
        if projected_port:
            status_ports.append(projected_port)
        else:
            flags.nested_dropped += 1
    return _compact(
        {
            **meta,
            "fleet": _string(_mapping(raw_meta.get("labels")).get(_FLEET_LABEL), _K8S_OBJECT_NAME),
            "state": _enum(status.get("state"), _GAMESERVER_STATES),
            "spec": _project_gameserver_spec(spec, flags),
            "statusPorts": status_ports,
        }
    )


def project_fleet_autoscaler(obj: Any, flags: _Flags | None = None) -> dict[str, Any] | None:
    """Project one FleetAutoscaler.

    Buffer policies keep their numeric bounds. Every other policy keeps only
    its type: webhook URLs and services are endpoints and are never read.
    """
    resource = _mapping(obj)
    meta = _metadata(resource)
    if meta is None:
        return None
    spec = _mapping(resource.get("spec"))
    policy = _mapping(spec.get("policy"))
    policy_type = _enum(policy.get("type"), _POLICY_TYPES)
    buffer: dict[str, Any] = {}
    if policy_type == "Buffer":
        raw_buffer = _mapping(policy.get("buffer"))
        size = raw_buffer.get("bufferSize")
        buffer = _compact(
            {
                "bufferSize": _int(size, 0, _MAX_REPLICAS) if not isinstance(size, str) else _string(size, _PERCENT),
                "minReplicas": _int(raw_buffer.get("minReplicas"), 0, _MAX_REPLICAS),
                "maxReplicas": _int(raw_buffer.get("maxReplicas"), 0, _MAX_REPLICAS),
            }
        )
    sync = _mapping(spec.get("sync"))
    return _compact(
        {
            **meta,
            "fleetName": _string(spec.get("fleetName"), _K8S_OBJECT_NAME),
            "policyType": policy_type,
            "buffer": buffer,
            "detailsOmitted": True if policy_type not in (None, "Buffer") else None,
            "sync": _compact(
                {
                    "type": _enum(sync.get("type"), _SYNC_TYPES),
                    "fixedIntervalSeconds": _int(_mapping(sync.get("fixedInterval")).get("seconds"), 1, _MAX_SECONDS),
                }
            ),
        }
    )


_PROJECTORS = {
    _SECTION_FLEETS: project_fleet,
    _SECTION_AUTOSCALERS: project_fleet_autoscaler,
    _SECTION_GAMESERVERS: project_gameserver,
}


# --------------------------------------------------------------------------- #
# Error classification (bounded prefix, text discarded)                       #
# --------------------------------------------------------------------------- #
_READ_DENIED = "denied"
_READ_STALE = "stale"
_READ_UNAVAILABLE = "unavailable"
_READ_MALFORMED = "malformed"
_READ_SKIPPED = "skipped"


# eks-mcp-server error prefixes: "Failed to {operation} {kind} {ns}/{name}: "
# for manage_k8s_resource and "Failed to list {kind} resources: " for
# list_k8s_resources. The object name and namespace sit inside this prefix, so
# it is removed before any marker is scanned.
_SERVER_ERROR_PREFIX = re.compile(r"\AFailed to [a-z]+ [A-Za-z]+(?: [^:\s]*)?: ")
# A Kubernetes ApiException renders as "(403)\nReason: Forbidden ...".
_STATUS_MARKER = re.compile(r"\A\s*\((\d{3})\)")
_DENIED_STATUS = frozenset({"401", "403"})
_STALE_STATUS = frozenset({"404"})


def _classify_failure(result: Mapping[str, Any], identifiers: Sequence[str] = ()) -> str:
    """Return a typed failure code from a bounded prefix; the text is discarded.

    Object names cannot steer the code. The server's known prefix, which
    carries the namespace and name, is removed first. A leading HTTP status
    marker such as ``(403)`` or ``(404)`` then decides. Otherwise the words are
    scanned after every caller-known identifier (cluster, namespace, object
    name) is blanked out, because a timeout message can repeat the request URL.

    ``not found`` after a successful list means the object went away between
    list and read, so the listing is stale rather than unavailable.
    """
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                text = _SERVER_ERROR_PREFIX.sub("", item["text"][:_CLASSIFY_ERROR_PREFIX], count=1)
                status = _STATUS_MARKER.match(text)
                if status:
                    if status.group(1) in _DENIED_STATUS:
                        return _READ_DENIED
                    if status.group(1) in _STALE_STATUS:
                        return _READ_STALE
                    return _READ_UNAVAILABLE
                for identifier in sorted((i for i in identifiers if i), key=len, reverse=True):
                    text = text.replace(identifier, " ")
                low = text.lower()
                if "forbidden" in low or "accessdenied" in low or "not authorized" in low or "unauthorized" in low:
                    return _READ_DENIED
                if "not found" in low or "notfound" in low:
                    return _READ_STALE
                break
    return _READ_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Depth and size bounds                                                       #
# --------------------------------------------------------------------------- #
def nesting_depth(value: Any) -> int:
    """Container nesting depth: a scalar is 0, ``{}``/``[]`` count one level."""
    if isinstance(value, Mapping):
        return 1 + max((nesting_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((nesting_depth(v) for v in value), default=0)
    return 0


def _encoded_size(envelope: Mapping[str, Any]) -> int:
    return len(_dumps(envelope).encode("utf-8"))


def _trim_largest_section(envelope: dict[str, Any]) -> bool:
    """Halve the largest section's items. Returns False when nothing is left."""
    best_key: str | None = None
    best_len = 0
    for key, _, _, _ in _READ_PLAN:
        section = envelope.get(key)
        if isinstance(section, dict) and isinstance(section.get("items"), list):
            if len(section["items"]) > best_len:
                best_key, best_len = key, len(section["items"])
    if best_key is None:
        return False
    section = envelope[best_key]
    section["items"] = section["items"][: best_len // 2]
    section["returned"] = len(section["items"])
    section["truncated"] = True
    section["hasMore"] = True
    if section.get("outcome") in (OUTCOME_COMPLETE, OUTCOME_EMPTY):
        section["outcome"] = OUTCOME_TRUNCATED
    envelope["truncated"] = True
    if envelope.get("outcome") in (OUTCOME_COMPLETE, OUTCOME_EMPTY):
        envelope["outcome"] = OUTCOME_TRUNCATED
    return True


def finalize_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Enforce the depth and byte caps on the final envelope.

    Trims the largest section deterministically before withholding the whole
    inventory with an explicit ``truncated`` envelope.
    """
    if nesting_depth(envelope) > MAX_DEPTH:
        return {
            "v": AGONES_ENVELOPE_VERSION,
            "operation": AGONES_INVENTORY_TOOL,
            "outcome": OUTCOME_MALFORMED,
            "errorCode": "AGONES_MALFORMED",
            "message": "The Agones inventory could not be bounded safely.",
        }
    for _ in range(24):
        if _encoded_size(envelope) <= MAX_ENVELOPE_BYTES:
            return envelope
        if not _trim_largest_section(envelope):
            break
    return {
        "v": AGONES_ENVELOPE_VERSION,
        "operation": AGONES_INVENTORY_TOOL,
        "outcome": OUTCOME_TRUNCATED,
        "truncated": True,
        "hasMore": True,
        "message": _MSG_WITHHELD,
    }


# --------------------------------------------------------------------------- #
# Section assembly                                                            #
# --------------------------------------------------------------------------- #
def _section_outcome(
    selected: int, returned: int, failures: Mapping[str, int], has_more: bool, list_partial: bool
) -> str:
    failed = sum(failures.values())
    if selected == 0:
        if list_partial:
            return OUTCOME_PARTIAL
        return OUTCOME_TRUNCATED if has_more else OUTCOME_EMPTY
    if returned == 0:
        for code, outcome in (
            (_READ_DENIED, OUTCOME_DENIED),
            (_READ_MALFORMED, OUTCOME_MALFORMED),
        ):
            if failures.get(code, 0) == selected:
                return outcome
        if failures.get(_READ_UNAVAILABLE, 0) + failures.get(_READ_SKIPPED, 0) == selected:
            return OUTCOME_UNAVAILABLE
        return OUTCOME_PARTIAL
    if failed or list_partial:
        return OUTCOME_PARTIAL
    return OUTCOME_TRUNCATED if has_more else OUTCOME_COMPLETE


def _list_names(
    payload: Mapping[str, Any], kind: str, api_version: str
) -> tuple[list[tuple[str, str | None]], int, bool]:
    """Validate a list payload and return ``(names, listed_count, partial)``.

    Raises ``ValueError`` when the payload is not the expected list shape.
    """
    items = payload.get("items")
    count = payload.get("count")
    if (
        not isinstance(items, list)
        or payload.get("kind") != kind
        or payload.get("api_version") != api_version
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        raise ValueError("unexpected list shape")
    partial = count != len(items)
    keys: set[tuple[str, str]] = set()
    for item in items[:MAX_LISTED_NAMES]:
        summary = _mapping(item)
        name = _string(summary.get("name"), _K8S_OBJECT_NAME)
        raw_namespace = summary.get("namespace")
        namespace = _string(raw_namespace, _NAMESPACE) if raw_namespace is not None else None
        if name is None or (raw_namespace is not None and namespace is None):
            partial = True
            continue
        keys.add((namespace or "", name))
    if len(items) > MAX_LISTED_NAMES:
        partial = True
    # Deterministic order: namespace, then name.
    return [(name, namespace or None) for namespace, name in sorted(keys)], count, partial


class AgonesInventoryTool(AgentTool):
    """Code-owned, read-only Agones inventory over the guarded EKS MCP path."""

    def __init__(
        self, list_delegate: AgentTool, read_delegate: AgentTool, versions_delegate: AgentTool | None = None
    ) -> None:
        super().__init__()
        self._list = list_delegate
        self._read = read_delegate
        self._versions = versions_delegate
        self._calls = 0

    @property
    def tool_name(self) -> str:
        return AGONES_INVENTORY_TOOL

    @property
    def tool_type(self) -> str:
        return "python"

    @property
    def tool_spec(self) -> ToolSpec:
        return {
            "name": AGONES_INVENTORY_TOOL,
            "description": (
                "Read-only, bounded Agones inventory for one enrolled EKS cluster: Fleets, FleetAutoscalers, "
                "and a sample of GameServers with their replica, allocation, scaling, port, image, and Region "
                "facts. Returns a versioned envelope whose outcome is complete, empty, denied, unavailable, "
                "malformed, partial, or truncated, or not_installed when the cluster does not serve the "
                "Agones API. Creates or changes nothing."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "cluster_name": {
                            "type": "string",
                            "description": "Name of an enrolled EKS cluster.",
                            "maxLength": 100,
                        },
                        "namespace": {
                            "type": "string",
                            "description": "Optional Kubernetes namespace to narrow the read.",
                            "maxLength": 63,
                        },
                    },
                    "required": ["cluster_name"],
                    "additionalProperties": False,
                }
            },
        }

    def _error(self, tool_use_id: str, outcome: str, code: str, message: str) -> ToolResultEvent:
        body = {
            "v": AGONES_ENVELOPE_VERSION,
            "operation": AGONES_INVENTORY_TOOL,
            "outcome": outcome,
            "errorCode": code,
            "message": message,
        }
        _log_outcome(AGONES_INVENTORY_TOOL, code)
        return ToolResultEvent({"toolUseId": tool_use_id, "status": "error", "content": [{"text": _dumps(body)}]})

    def _validate(self, inp: Any) -> tuple[str, str | None] | None:
        if not isinstance(inp, Mapping) or set(inp) - {"cluster_name", "namespace"}:
            return None
        cluster = inp.get("cluster_name")
        if not isinstance(cluster, str) or not _EKS_NAME.match(cluster):
            return None
        if any(shape.search(cluster) for shape in _SENSITIVE_SCALAR_SHAPES):
            return None
        namespace = inp.get("namespace")
        if namespace is not None and (not isinstance(namespace, str) or not _NAMESPACE.match(namespace)):
            return None
        return cluster, namespace

    async def _call(
        self,
        delegate: AgentTool,
        tool_use_id: str,
        tool_input: dict[str, Any],
        invocation_state: dict[str, Any],
        remaining: float,
        **kwargs: Any,
    ) -> Mapping[str, Any] | None:
        self._calls += 1
        use: ToolUse = {
            "toolUseId": f"{tool_use_id}-agones-{self._calls}",
            "name": delegate.tool_name,
            "input": tool_input,
        }
        return await _drain_final_result(delegate, use, invocation_state, deadline_seconds=remaining, **kwargs)

    async def _agones_installed(
        self, tool_use_id: str, cluster: str, invocation_state: dict[str, Any], deadline: float, **kwargs: Any
    ) -> bool | None:
        """``False`` only when the cluster answered and lacks ``agones.dev/v1``.

        ``None`` means the check could not decide (no delegate, error,
        timeout, or an unexpected shape); the caller then reads as usual.
        """
        if self._versions is None:
            return None
        remaining = deadline - asyncio.get_running_loop().time()
        result = await self._call(
            self._versions, tool_use_id, {"cluster_name": cluster}, invocation_state, remaining, **kwargs
        )
        if result is None or result.get("status") == "error":
            return None
        payload = _extract_eks_payload(result)
        versions = _mapping(payload).get("api_versions")
        if not isinstance(versions, list) or not versions or len(versions) > MAX_API_VERSIONS:
            return None
        return AGONES_API_VERSION in versions

    async def _section(
        self,
        tool_use_id: str,
        section: str,
        kind: str,
        api_version: str,
        cap: int,
        cluster: str,
        namespace: str | None,
        invocation_state: dict[str, Any],
        deadline: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {"outcome": OUTCOME_UNAVAILABLE, "items": [], "returned": 0, "failures": {_READ_SKIPPED: 1}}

        list_input: dict[str, Any] = {"cluster_name": cluster, "kind": kind, "api_version": api_version}
        if namespace:
            list_input["namespace"] = namespace
        listed = await self._call(self._list, tool_use_id, list_input, invocation_state, remaining, **kwargs)
        if listed is None:
            return {"outcome": OUTCOME_UNAVAILABLE, "items": [], "returned": 0}
        if listed.get("status") == "error":
            code = _classify_failure(listed, (cluster, namespace or ""))
            outcome = OUTCOME_DENIED if code == _READ_DENIED else OUTCOME_UNAVAILABLE
            return {"outcome": outcome, "items": [], "returned": 0}
        payload = _extract_eks_payload(listed)
        try:
            if payload is None:
                raise ValueError("no payload")
            names, listed_count, list_partial = _list_names(payload, kind, api_version)
        except ValueError:
            return {"outcome": OUTCOME_MALFORMED, "items": [], "returned": 0}

        selected = names[:cap]
        has_more = len(names) > cap
        failures: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        flags = _Flags()
        projector = _PROJECTORS[section]
        for name, item_namespace in selected:
            remaining = deadline - loop.time()
            if remaining <= 0:
                failures[_READ_SKIPPED] = failures.get(_READ_SKIPPED, 0) + 1
                continue
            read_input: dict[str, Any] = {
                "operation": READ_OPERATION,
                "cluster_name": cluster,
                "kind": kind,
                "api_version": api_version,
                "name": name,
            }
            if item_namespace:
                read_input["namespace"] = item_namespace
            result = await self._call(self._read, tool_use_id, read_input, invocation_state, remaining, **kwargs)
            if result is None:
                failures[_READ_UNAVAILABLE] = failures.get(_READ_UNAVAILABLE, 0) + 1
                continue
            if result.get("status") == "error":
                code = _classify_failure(result, (cluster, item_namespace or "", name))
                failures[code] = failures.get(code, 0) + 1
                continue
            data = _extract_eks_payload(result)
            resource = _mapping(data).get("resource") if data is not None else None
            projected = None
            if isinstance(resource, Mapping) and _mapping(data).get("kind") == kind:
                try:
                    projected = projector(resource, flags)
                except Exception:  # noqa: BLE001 - projection fault is a typed malformed outcome
                    projected = None
            if projected is None:
                failures[_READ_MALFORMED] = failures.get(_READ_MALFORMED, 0) + 1
                continue
            items.append(projected)

        outcome = _section_outcome(len(selected), len(items), failures, has_more, list_partial)
        if flags.nested_dropped and outcome in (OUTCOME_COMPLETE, OUTCOME_TRUNCATED):
            # A dropped nested entry makes the view partial, never complete.
            outcome = OUTCOME_PARTIAL
        elif outcome == OUTCOME_COMPLETE and flags.nested_truncated:
            outcome = OUTCOME_TRUNCATED
        out: dict[str, Any] = {
            "outcome": outcome,
            "listed": listed_count,
            "returned": len(items),
            "items": items,
        }
        if has_more:
            out["hasMore"] = True
            out["truncated"] = True
        if flags.nested_truncated:
            out["truncated"] = True
            out["nestedTruncated"] = True
        if flags.nested_dropped:
            out["nestedDropped"] = flags.nested_dropped
        if failures:
            out["failures"] = dict(sorted(failures.items()))
        if failures.get(_READ_STALE):
            out["stale"] = True
        if outcome == OUTCOME_PARTIAL:
            out["partial"] = True
        if section == _SECTION_GAMESERVERS:
            counts: dict[str, int] = {}
            for item in items:
                state = item.get("state", "Unknown")
                counts[state] = counts.get(state, 0) + 1
            out["stateCounts"] = dict(sorted(counts.items()))
            if has_more:
                out["sampled"] = True
        return out

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        validated = self._validate(tool_use.get("input"))
        if validated is None:
            yield self._error(tool_use_id, OUTCOME_DENIED, _CODE_DENIED_INPUT, _MSG_BAD_INPUT)
            return
        cluster, namespace = validated
        if cluster not in enrolled_clusters():
            yield self._error(tool_use_id, OUTCOME_DENIED, _CODE_NOT_ENROLLED, _MSG_NOT_ENROLLED)
            return

        deadline = asyncio.get_running_loop().time() + INVENTORY_DEADLINE_SECONDS
        if await self._agones_installed(tool_use_id, cluster, invocation_state, deadline, **kwargs) is False:
            yield self._error(tool_use_id, OUTCOME_NOT_INSTALLED, _CODE_NOT_INSTALLED, _MSG_NOT_INSTALLED)
            return
        envelope: dict[str, Any] = {
            "v": AGONES_ENVELOPE_VERSION,
            "operation": AGONES_INVENTORY_TOOL,
            "readOnly": True,
            "scope": _compact({"clusterName": cluster, "namespace": namespace, "region": _runtime_region()}),
        }
        outcomes: list[str] = []
        for section, kind, api_version, cap in _READ_PLAN:
            result = await self._section(
                tool_use_id, section, kind, api_version, cap, cluster, namespace, invocation_state, deadline, **kwargs
            )
            envelope[section] = result
            outcomes.append(result["outcome"])

        envelope["outcome"] = combine_outcomes(outcomes)
        flags = {key for section, *_ in _READ_PLAN for key in envelope[section] if key in ("stale", "truncated")}
        for flag in sorted(flags):
            envelope[flag] = True
        if envelope["outcome"] == OUTCOME_PARTIAL:
            envelope["partial"] = True
        for section, *_ in _READ_PLAN:
            _log_outcome(AGONES_INVENTORY_TOOL, envelope[section]["outcome"])
        final = finalize_envelope(envelope)
        yield ToolResultEvent({"toolUseId": tool_use_id, "status": "success", "content": [{"text": _dumps(final)}]})


def combine_outcomes(outcomes: Sequence[str]) -> str:
    """Combine section outcomes without hiding a failure.

    Identical outcomes keep their value. A mix of complete/empty is complete.
    A mix that adds truncation is truncated. Any other mix is partial.
    """
    unique = set(outcomes)
    if len(unique) == 1:
        return outcomes[0]
    if unique <= {OUTCOME_COMPLETE, OUTCOME_EMPTY}:
        return OUTCOME_COMPLETE
    if unique <= {OUTCOME_COMPLETE, OUTCOME_EMPTY, OUTCOME_TRUNCATED}:
        return OUTCOME_TRUNCATED
    return OUTCOME_PARTIAL


__all__ = [
    "AGONES_API_VERSION",
    "AGONES_ENVELOPE_VERSION",
    "AGONES_READ_KINDS",
    "AgonesInventoryTool",
    "INVENTORY_DEADLINE_SECONDS",
    "MAX_AUTOSCALERS",
    "MAX_CONTAINERS",
    "MAX_DEPTH",
    "MAX_ENVELOPE_BYTES",
    "MAX_FLEETS",
    "MAX_GAMESERVERS",
    "MAX_PORTS",
    "MAX_STRING",
    "OUTCOME_NOT_INSTALLED",
    "READ_OPERATION",
    "combine_outcomes",
    "enrolled_clusters",
    "finalize_envelope",
    "nesting_depth",
    "project_fleet",
    "project_fleet_autoscaler",
    "project_gameserver",
]
