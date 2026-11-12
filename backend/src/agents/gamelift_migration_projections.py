"""Bounded GameLift projections for the read-only migration advisor (#529).

These extend the #464 fleet-list projections in ``gamelift_projections`` with
the queue, location, container-definition, and container-port facts an
Agones-to-GameLift assessment needs. They reuse that module's validators,
status vocabulary, typed error codes, and collection bounding rather than
adding a parallel framework.

Every model-visible value is copied from an explicit allowlist and validated.
The following are dropped by construction: every ARN (queue, fleet, role,
container group definition, SNS target), account identifiers, image registry
hosts, inbound IP ranges, environment overrides, mount points, health-check
commands, Linux capabilities, free-text descriptions, ``StatusReason``, and
``CustomEventData``.

A queue destination is reported as a fleet or alias ID plus its Region, parsed
deterministically from the destination ARN. A destination that does not parse
is counted, never guessed.
"""

from __future__ import annotations

# Standard library
import re
from typing import Any

# Local modules
from agents.gamelift_projections import (
    _CONTAINER_GROUP_FIELDS,
    _NAME,
    _NUMBER,
    _REGION,
    _TIMESTAMP,
    ERROR_MALFORMED_RESPONSE,
    GAMELIFT_MAX_PROJECTED_CHARS,
    STATUS_INCOMPLETE,
    STATUS_OK,
    STATUS_TRUNCATED,
    _copy_validated,
    _project_collection,
    _serialized_length,
    _valid_fleet_id,
    _valid_number,
    _valid_typed_string,
)
from agents.image_reference import project_image_reference

MAX_QUEUES = 25
MAX_QUEUE_DESTINATIONS = 20
MAX_LATENCY_POLICIES = 10
MAX_QUEUE_LOCATIONS = 20
MAX_FLEET_LOCATIONS = 100
MAX_PORT_RANGES = 10
MAX_INBOUND_PERMISSIONS = 20
MAX_SUPPORT_CONTAINERS = 8
MAX_CONTAINER_GROUP_DEFINITION_NAME = 128

_PRIORITY_TYPES = frozenset({"LATENCY", "COST", "DESTINATION", "LOCATION"})
_IP_PROTOCOLS = frozenset({"TCP", "UDP"})
_FLEET_LOCATION_STATUS = frozenset(
    {
        "NEW",
        "DOWNLOADING",
        "VALIDATING",
        "BUILDING",
        "ACTIVATING",
        "ACTIVE",
        "DELETING",
        "ERROR",
        "TERMINATED",
        "NOT_FOUND",
        "EXPIRED",
    }
)
_CONTAINER_LOCATION_STATUS = frozenset(
    {"PENDING", "CREATING", "CREATED", "ACTIVATING", "ACTIVE", "UPDATING", "DELETING", "EXPIRED"}
)
_PLAYER_GATEWAY_STATUS = frozenset({"DISABLED", "ENABLED"})
_LOCATION_UPDATE_STATUS = frozenset({"PENDING_UPDATE"})
_CONTAINER_BILLING = frozenset({"ON_DEMAND", "SPOT"})

_DESTINATION_ARN = re.compile(r"^arn:aws[a-z-]*:gamelift:([a-z0-9-]{1,32}):\d{12}:(fleet|alias)/([A-Za-z0-9-]{1,128})$")
_ALIAS_ID = re.compile(r"^alias-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONTAINER_GROUP_DEFINITION_NAME = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_SDK_VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
_OPEN_RANGES = frozenset({"0.0.0.0/0", "::/0"})
_MAX_PORT = 65_535


def valid_container_group_definition_name(value: Any) -> bool:
    """A plain definition name (never an ARN) within the service grammar."""
    return (
        isinstance(value, str)
        and _CONTAINER_GROUP_DEFINITION_NAME.match(value) is not None
        and _valid_typed_string(value, _NAME)
    )


def _port(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_PORT else None


def _regions(value: Any, cap: int) -> tuple[list[str], bool]:
    """Validated Region list and whether the cap or a bad value cut it short."""
    if not isinstance(value, list):
        return [], False
    kept = [item for item in value[:cap] if _valid_typed_string(item, _REGION)]
    return kept, len(value) > cap or len(kept) != min(len(value), cap)


# --------------------------------------------------------------------------- #
# Game session queues                                                         #
# --------------------------------------------------------------------------- #
def project_queue_destination(value: Any) -> dict[str, Any] | None:
    """Return ``{Type, FleetId|AliasId, Region}`` from a destination ARN.

    The ARN itself, including its account ID, is never returned. ``None`` means
    the destination did not parse and must be counted as unresolved.
    """
    if not isinstance(value, dict):
        return None
    arn = value.get("DestinationArn")
    if not isinstance(arn, str):
        return None
    match = _DESTINATION_ARN.match(arn)
    if match is None:
        return None
    region, resource_type, resource_id = match.groups()
    if not _valid_typed_string(region, _REGION):
        return None
    if resource_type == "fleet":
        if not _valid_fleet_id(resource_id):
            return None
        return {"Type": "fleet", "FleetId": resource_id, "Region": region}
    if _ALIAS_ID.match(resource_id) is None:
        return None
    return {"Type": "alias", "AliasId": resource_id, "Region": region}


def project_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    """Project one ``GameSessionQueue``. Rows without a valid name are dropped."""
    projected = _copy_validated(item, (("Name", _NAME), ("TimeoutInSeconds", _NUMBER)))
    if "Name" not in projected:
        return {}

    raw_destinations = item.get("Destinations")
    if isinstance(raw_destinations, list):
        destinations: list[dict[str, Any]] = []
        unresolved = 0
        for raw in raw_destinations[:MAX_QUEUE_DESTINATIONS]:
            destination = project_queue_destination(raw)
            if destination is None:
                unresolved += 1
            else:
                destinations.append(destination)
        projected["Destinations"] = destinations
        if unresolved:
            projected["UnresolvedDestinationCount"] = unresolved
        if len(raw_destinations) > MAX_QUEUE_DESTINATIONS:
            projected["DestinationsTruncated"] = True

    raw_policies = item.get("PlayerLatencyPolicies")
    if isinstance(raw_policies, list):
        policies = []
        for raw in raw_policies[:MAX_LATENCY_POLICIES]:
            if isinstance(raw, dict):
                policy = _copy_validated(
                    raw,
                    (("MaximumIndividualPlayerLatencyMilliseconds", _NUMBER), ("PolicyDurationSeconds", _NUMBER)),
                )
                if policy:
                    policies.append(policy)
        if policies:
            projected["PlayerLatencyPolicies"] = policies
        if len(raw_policies) > MAX_LATENCY_POLICIES:
            projected["PlayerLatencyPoliciesTruncated"] = True

    priority = item.get("PriorityConfiguration")
    if isinstance(priority, dict):
        out: dict[str, Any] = {}
        order = priority.get("PriorityOrder")
        if isinstance(order, list):
            kept = [value for value in order[: len(_PRIORITY_TYPES)] if value in _PRIORITY_TYPES]
            if kept:
                out["PriorityOrder"] = kept
        locations, cut = _regions(priority.get("LocationOrder"), MAX_QUEUE_LOCATIONS)
        if locations:
            out["LocationOrder"] = locations
        if cut:
            out["LocationOrderTruncated"] = True
        if out:
            projected["PriorityConfiguration"] = out

    filters = item.get("FilterConfiguration")
    if isinstance(filters, dict):
        allowed, cut = _regions(filters.get("AllowedLocations"), MAX_QUEUE_LOCATIONS)
        if allowed or cut:
            projected["FilterConfiguration"] = {"AllowedLocations": allowed}
            if cut:
                projected["FilterConfiguration"]["AllowedLocationsTruncated"] = True
    return projected


def project_game_session_queues(items: list[Any], residual: bool, pages_malformed: bool) -> dict[str, Any]:
    """Bound and classify a ``DescribeGameSessionQueues`` read.

    ``residual`` means the bounded paginator left queues unread: that is a
    ``truncated`` + ``paginated`` result, not an empty or complete one.
    """
    response: dict[str, Any] = {"GameSessionQueues": items}
    if residual:
        response["NextToken"] = "residual"
    result = _project_collection(response, "GameSessionQueues", project_queue_item)
    if residual and result["status"] == STATUS_INCOMPLETE and "error" not in result:
        result["status"] = STATUS_TRUNCATED
        result["truncated"] = True
        result["paginated"] = True
    if pages_malformed and "error" not in result:
        result["status"] = STATUS_INCOMPLETE
        result["truncated"] = True
        result["error"] = {"code": ERROR_MALFORMED_RESPONSE}
    return result


# --------------------------------------------------------------------------- #
# Fleet locations                                                             #
# --------------------------------------------------------------------------- #
def _project_classic_location(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("LocationState")
    if not isinstance(state, dict):
        return {}
    projected = _copy_validated(state, (("Location", _REGION),))
    if "Location" not in projected:
        return {}
    status = state.get("Status")
    if status in _FLEET_LOCATION_STATUS:
        projected["Status"] = status
    gateway = state.get("PlayerGatewayStatus")
    if gateway in _PLAYER_GATEWAY_STATUS:
        projected["PlayerGatewayStatus"] = gateway
    update = item.get("UpdateStatus")
    if update in _LOCATION_UPDATE_STATUS:
        projected["UpdateStatus"] = update
    return projected


def _project_container_location(item: dict[str, Any]) -> dict[str, Any]:
    projected = _copy_validated(item, (("Location", _REGION),))
    if "Location" not in projected:
        return {}
    status = item.get("Status")
    if status in _CONTAINER_LOCATION_STATUS:
        projected["Status"] = status
    gateway = item.get("PlayerGatewayStatus")
    if gateway in _PLAYER_GATEWAY_STATUS:
        projected["PlayerGatewayStatus"] = gateway
    return projected


def project_classic_fleet_locations(response: Any) -> dict[str, Any]:
    """Project ``DescribeFleetLocationAttributes`` into ``Locations``."""
    if not isinstance(response, dict):
        return _project_collection(response, "Locations", _project_classic_location)
    raw = response.get("LocationAttributes", [])
    shaped: dict[str, Any] = {"Locations": raw}
    if response.get("NextToken"):
        shaped["NextToken"] = response["NextToken"]
    return _project_collection(shaped, "Locations", _project_classic_location)


def project_container_fleet_locations(response: Any) -> dict[str, Any]:
    """Project ``DescribeContainerFleet`` location attributes into ``Locations``."""
    fleet = response.get("ContainerFleet") if isinstance(response, dict) else None
    if not isinstance(fleet, dict):
        return {"status": STATUS_INCOMPLETE, "Locations": [], "error": {"code": ERROR_MALFORMED_RESPONSE}}
    raw = fleet.get("LocationAttributes", [])
    return _project_collection({"Locations": raw}, "Locations", _project_container_location)


# --------------------------------------------------------------------------- #
# Container group definitions                                                 #
# --------------------------------------------------------------------------- #
def _project_port_ranges(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, dict):
        return [], False
    ranges = value.get("ContainerPortRanges")
    if not isinstance(ranges, list):
        return [], False
    kept: list[dict[str, Any]] = []
    for raw in ranges[:MAX_PORT_RANGES]:
        if not isinstance(raw, dict):
            continue
        from_port, to_port = _port(raw.get("FromPort")), _port(raw.get("ToPort"))
        protocol = raw.get("Protocol")
        if from_port is None or to_port is None or from_port > to_port or protocol not in _IP_PROTOCOLS:
            continue
        kept.append({"FromPort": from_port, "ToPort": to_port, "Protocol": protocol})
    return kept, len(ranges) > MAX_PORT_RANGES


def _project_container(value: Any, support: bool) -> tuple[dict[str, Any], bool]:
    """Project one container definition. Env, mounts, and health checks are dropped."""
    if not isinstance(value, dict):
        return {}, False
    out: dict[str, Any] = {}
    name = value.get("ContainerName")
    if isinstance(name, str) and _CONTAINER_NAME.match(name) and _valid_typed_string(name, _NAME):
        out["ContainerName"] = name
    image = project_image_reference(value.get("ImageUri"))
    if image is not None:
        out["Image"] = image
    digest = value.get("ResolvedImageDigest")
    if isinstance(digest, str) and _SHA256.match(digest):
        out["ResolvedImageDigest"] = digest
    vcpu = value.get("Vcpu")
    if vcpu is not None and _valid_number(vcpu):
        out["Vcpu"] = vcpu
    ports, cut = _project_port_ranges(value.get("PortConfiguration"))
    if ports:
        out["PortRanges"] = ports
    if cut:
        out["PortRangesTruncated"] = True
    if support:
        memory = value.get("MemoryHardLimitMebibytes")
        if memory is not None and _valid_number(memory):
            out["MemoryHardLimitMebibytes"] = memory
        essential = value.get("Essential")
        if isinstance(essential, bool):
            out["Essential"] = essential
    else:
        sdk = value.get("ServerSdkVersion")
        if isinstance(sdk, str) and _SDK_VERSION.match(sdk):
            out["ServerSdkVersion"] = sdk
    return out, cut


def project_container_group_definition(response: Any) -> dict[str, Any]:
    """Project ``DescribeContainerGroupDefinition`` into a bounded envelope."""
    definition = response.get("ContainerGroupDefinition") if isinstance(response, dict) else None
    if not isinstance(definition, dict):
        return {
            "status": STATUS_INCOMPLETE,
            "ContainerGroupDefinition": {},
            "error": {"code": ERROR_MALFORMED_RESPONSE},
        }
    projected = _copy_validated(definition, _CONTAINER_GROUP_FIELDS + (("CreationTime", _TIMESTAMP),))
    if "Name" not in projected:
        return {
            "status": STATUS_INCOMPLETE,
            "ContainerGroupDefinition": {},
            "error": {"code": ERROR_MALFORMED_RESPONSE},
        }

    truncated = False
    game_server, cut = _project_container(definition.get("GameServerContainerDefinition"), support=False)
    truncated = truncated or cut
    if game_server:
        projected["GameServerContainerDefinition"] = game_server
    raw_support = definition.get("SupportContainerDefinitions")
    if isinstance(raw_support, list):
        support_rows = []
        for raw in raw_support[:MAX_SUPPORT_CONTAINERS]:
            row, cut = _project_container(raw, support=True)
            truncated = truncated or cut
            if row:
                support_rows.append(row)
        if support_rows:
            projected["SupportContainerDefinitions"] = support_rows
        if len(raw_support) > MAX_SUPPORT_CONTAINERS:
            truncated = True

    result: dict[str, Any] = {
        "status": STATUS_TRUNCATED if truncated else STATUS_OK,
        "ContainerGroupDefinition": projected,
    }
    if truncated:
        result["truncated"] = True
    while _serialized_length(result) > GAMELIFT_MAX_PROJECTED_CHARS:
        support_rows = projected.get("SupportContainerDefinitions")
        if not support_rows:
            return {
                "status": STATUS_TRUNCATED,
                "ContainerGroupDefinition": {"Name": projected["Name"]},
                "truncated": True,
            }
        support_rows.pop()
        result["status"] = STATUS_TRUNCATED
        result["truncated"] = True
    return result


# --------------------------------------------------------------------------- #
# Container fleet ports                                                       #
# --------------------------------------------------------------------------- #
def _inbound_scope(ip_range: Any) -> str | None:
    """Code-owned exposure token. The address range itself is never returned."""
    if not isinstance(ip_range, str):
        return None
    return "any-address" if ip_range.strip() in _OPEN_RANGES else "restricted"


def project_container_fleet_ports(response: Any) -> dict[str, Any]:
    """Project the port, inbound-permission, and packing facts of a container fleet."""
    fleet = response.get("ContainerFleet") if isinstance(response, dict) else None
    if not isinstance(fleet, dict):
        return {"status": STATUS_INCOMPLETE, "ContainerFleet": {}, "error": {"code": ERROR_MALFORMED_RESPONSE}}
    fleet_id = fleet.get("FleetId")
    if not _valid_fleet_id(fleet_id):
        return {"status": STATUS_INCOMPLETE, "ContainerFleet": {}, "error": {"code": ERROR_MALFORMED_RESPONSE}}
    projected: dict[str, Any] = {"FleetId": fleet_id}
    projected.update(
        _copy_validated(
            fleet,
            (
                ("GameServerContainerGroupsPerInstance", _NUMBER),
                ("MaximumGameServerContainerGroupsPerInstance", _NUMBER),
            ),
        )
    )
    for key in ("GameServerContainerGroupDefinitionName", "PerInstanceContainerGroupDefinitionName"):
        if valid_container_group_definition_name(fleet.get(key)):
            projected[key] = fleet[key]
    if fleet.get("BillingType") in _CONTAINER_BILLING:
        projected["BillingType"] = fleet["BillingType"]

    connection = fleet.get("InstanceConnectionPortRange")
    if isinstance(connection, dict):
        from_port, to_port = _port(connection.get("FromPort")), _port(connection.get("ToPort"))
        if from_port is not None and to_port is not None and from_port <= to_port:
            projected["InstanceConnectionPortRange"] = {"FromPort": from_port, "ToPort": to_port}

    truncated = False
    permissions = fleet.get("InstanceInboundPermissions")
    if isinstance(permissions, list):
        kept = []
        for raw in permissions[:MAX_INBOUND_PERMISSIONS]:
            if not isinstance(raw, dict):
                continue
            from_port, to_port = _port(raw.get("FromPort")), _port(raw.get("ToPort"))
            protocol = raw.get("Protocol")
            if from_port is None or to_port is None or from_port > to_port or protocol not in _IP_PROTOCOLS:
                continue
            row: dict[str, Any] = {"FromPort": from_port, "ToPort": to_port, "Protocol": protocol}
            scope = _inbound_scope(raw.get("IpRange"))
            if scope:
                row["Scope"] = scope
            kept.append(row)
        projected["InstanceInboundPermissions"] = kept
        truncated = len(permissions) > MAX_INBOUND_PERMISSIONS
    result: dict[str, Any] = {"status": STATUS_TRUNCATED if truncated else STATUS_OK, "ContainerFleet": projected}
    if truncated:
        result["truncated"] = True
    return result


__all__ = [
    "MAX_FLEET_LOCATIONS",
    "MAX_INBOUND_PERMISSIONS",
    "MAX_LATENCY_POLICIES",
    "MAX_PORT_RANGES",
    "MAX_QUEUES",
    "MAX_QUEUE_DESTINATIONS",
    "MAX_QUEUE_LOCATIONS",
    "MAX_SUPPORT_CONTAINERS",
    "project_classic_fleet_locations",
    "project_container_fleet_locations",
    "project_container_fleet_ports",
    "project_container_group_definition",
    "project_game_session_queues",
    "project_queue_destination",
    "project_queue_item",
    "valid_container_group_definition_name",
]
