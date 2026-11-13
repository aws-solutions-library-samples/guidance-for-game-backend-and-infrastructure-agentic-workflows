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

Nested lists follow the #464 rule that a mixed collection is never ``ok``. A
nested list cut at its cap makes an ``ok`` result ``truncated``. A nested entry
dropped because it failed validation makes the result ``incomplete`` with
``malformed_response`` and ``truncated``, even when every top-level row is
valid.
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
    STATUS_DENIED,
    STATUS_EMPTY,
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


class _Nested:
    """Records nested-list bound hits and validation drops for one projection."""

    def __init__(self) -> None:
        self.capped = False
        self.dropped = False


def _apply_nested(result: dict[str, Any], nested: _Nested, collection_key: str | None = None) -> dict[str, Any]:
    """Fold nested caps and drops into the result status.

    A drop wins over a cap: ``incomplete`` + ``malformed_response`` +
    ``truncated``. A cap alone moves ``ok`` to ``truncated`` and sets the
    ``truncated`` flag. ``denied`` and ``empty`` results carry no rows and are
    left unchanged. When ``collection_key`` is given, rows are trimmed from the
    tail again so the added keys still fit the serialized budget.
    """
    if result.get("status") in (STATUS_DENIED, STATUS_EMPTY):
        return result
    if nested.dropped:
        result["status"] = STATUS_INCOMPLETE
        result["truncated"] = True
        result.setdefault("error", {"code": ERROR_MALFORMED_RESPONSE})
    elif nested.capped:
        if result.get("status") == STATUS_OK:
            result["status"] = STATUS_TRUNCATED
        result["truncated"] = True
    rows = result.get(collection_key) if collection_key else None
    if isinstance(rows, list):
        while len(rows) > 1 and _serialized_length(result) > GAMELIFT_MAX_PROJECTED_CHARS:
            rows.pop()
            result["truncated"] = True
            if result.get("status") == STATUS_OK:
                result["status"] = STATUS_TRUNCATED
    return result


def _regions(value: Any, cap: int, nested: _Nested) -> tuple[list[str], bool]:
    """Validated Region list and whether the cap cut it short.

    An invalid entry is dropped and recorded on ``nested``; a list longer than
    ``cap`` is recorded as capped.
    """
    if not isinstance(value, list):
        if value is not None:
            nested.dropped = True
        return [], False
    kept = [item for item in value[:cap] if _valid_typed_string(item, _REGION)]
    capped = len(value) > cap
    if len(kept) != min(len(value), cap):
        nested.dropped = True
    if capped:
        nested.capped = True
    return kept, capped


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


_LATENCY_POLICY_FIELDS = (("MaximumIndividualPlayerLatencyMilliseconds", _NUMBER), ("PolicyDurationSeconds", _NUMBER))


def project_queue_item(item: dict[str, Any], nested: _Nested | None = None) -> dict[str, Any]:
    """Project one ``GameSessionQueue``. Rows without a valid name are dropped.

    Nested caps and drops are recorded on ``nested`` so the collection status
    can report them.
    """
    nested = nested if nested is not None else _Nested()
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
            nested.dropped = True
        if len(raw_destinations) > MAX_QUEUE_DESTINATIONS:
            projected["DestinationsTruncated"] = True
            nested.capped = True

    raw_policies = item.get("PlayerLatencyPolicies")
    if isinstance(raw_policies, list):
        policies = []
        for raw in raw_policies[:MAX_LATENCY_POLICIES]:
            if not isinstance(raw, dict):
                nested.dropped = True
                continue
            policy = _copy_validated(raw, _LATENCY_POLICY_FIELDS)
            present = {key for key, _ in _LATENCY_POLICY_FIELDS if raw.get(key) is not None}
            if not policy or set(policy) != present:
                # A policy with no valid value, or with a value that failed
                # validation, is not a faithful copy of the provider's policy.
                nested.dropped = True
            if policy:
                policies.append(policy)
        if policies:
            projected["PlayerLatencyPolicies"] = policies
        if len(raw_policies) > MAX_LATENCY_POLICIES:
            projected["PlayerLatencyPoliciesTruncated"] = True
            nested.capped = True

    priority = item.get("PriorityConfiguration")
    if isinstance(priority, dict):
        out: dict[str, Any] = {}
        order = priority.get("PriorityOrder")
        if isinstance(order, list):
            kept = [value for value in order[: len(_PRIORITY_TYPES)] if value in _PRIORITY_TYPES]
            if len(kept) != len(order):
                nested.dropped = True
            if kept:
                out["PriorityOrder"] = kept
        locations, cut = _regions(priority.get("LocationOrder"), MAX_QUEUE_LOCATIONS, nested)
        if locations:
            out["LocationOrder"] = locations
        if cut:
            out["LocationOrderTruncated"] = True
        if out:
            projected["PriorityConfiguration"] = out

    filters = item.get("FilterConfiguration")
    if isinstance(filters, dict):
        allowed, cut = _regions(filters.get("AllowedLocations"), MAX_QUEUE_LOCATIONS, nested)
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
    nested = _Nested()
    result = _project_collection(response, "GameSessionQueues", lambda item: project_queue_item(item, nested))
    if residual and result["status"] == STATUS_INCOMPLETE and "error" not in result:
        result["status"] = STATUS_TRUNCATED
        result["truncated"] = True
        result["paginated"] = True
    if pages_malformed and "error" not in result:
        result["status"] = STATUS_INCOMPLETE
        result["truncated"] = True
        result["error"] = {"code": ERROR_MALFORMED_RESPONSE}
    return _apply_nested(result, nested, "GameSessionQueues")


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
def _valid_port_range(raw: Any) -> dict[str, Any] | None:
    """``{FromPort, ToPort, Protocol}`` when the range is valid, else ``None``."""
    if not isinstance(raw, dict):
        return None
    from_port, to_port = _port(raw.get("FromPort")), _port(raw.get("ToPort"))
    protocol = raw.get("Protocol")
    if from_port is None or to_port is None or from_port > to_port or protocol not in _IP_PROTOCOLS:
        return None
    return {"FromPort": from_port, "ToPort": to_port, "Protocol": protocol}


def _project_port_ranges(value: Any, nested: _Nested) -> tuple[list[dict[str, Any]], bool]:
    if value is None:
        return [], False
    ranges = value.get("ContainerPortRanges") if isinstance(value, dict) else None
    if not isinstance(ranges, list):
        nested.dropped = True
        return [], False
    kept: list[dict[str, Any]] = []
    for raw in ranges[:MAX_PORT_RANGES]:
        port_range = _valid_port_range(raw)
        if port_range is None:
            nested.dropped = True
            continue
        kept.append(port_range)
    capped = len(ranges) > MAX_PORT_RANGES
    if capped:
        nested.capped = True
    return kept, capped


def _project_container(value: Any, support: bool, nested: _Nested) -> tuple[dict[str, Any], bool]:
    """Project one container definition. Env, mounts, and health checks are dropped."""
    if not isinstance(value, dict):
        if value is not None:
            nested.dropped = True
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
    ports, cut = _project_port_ranges(value.get("PortConfiguration"), nested)
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

    nested = _Nested()
    game_server, _ = _project_container(definition.get("GameServerContainerDefinition"), False, nested)
    if game_server:
        projected["GameServerContainerDefinition"] = game_server
    raw_support = definition.get("SupportContainerDefinitions")
    if isinstance(raw_support, list):
        support_rows = []
        for raw in raw_support[:MAX_SUPPORT_CONTAINERS]:
            row, _ = _project_container(raw, True, nested)
            if row:
                support_rows.append(row)
            else:
                nested.dropped = True
        if support_rows:
            projected["SupportContainerDefinitions"] = support_rows
        if len(raw_support) > MAX_SUPPORT_CONTAINERS:
            nested.capped = True
    elif raw_support is not None:
        nested.dropped = True

    result: dict[str, Any] = _apply_nested({"status": STATUS_OK, "ContainerGroupDefinition": projected}, nested)
    while _serialized_length(result) > GAMELIFT_MAX_PROJECTED_CHARS:
        support_rows = projected.get("SupportContainerDefinitions")
        if not support_rows:
            return {
                "status": STATUS_TRUNCATED,
                "ContainerGroupDefinition": {"Name": projected["Name"]},
                "truncated": True,
            }
        support_rows.pop()
        if result["status"] == STATUS_OK:
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

    nested = _Nested()
    connection = fleet.get("InstanceConnectionPortRange")
    if connection is not None:
        from_port = _port(connection.get("FromPort")) if isinstance(connection, dict) else None
        to_port = _port(connection.get("ToPort")) if isinstance(connection, dict) else None
        if from_port is not None and to_port is not None and from_port <= to_port:
            projected["InstanceConnectionPortRange"] = {"FromPort": from_port, "ToPort": to_port}
        else:
            nested.dropped = True

    permissions = fleet.get("InstanceInboundPermissions")
    if isinstance(permissions, list):
        kept = []
        for raw in permissions[:MAX_INBOUND_PERMISSIONS]:
            row = _valid_port_range(raw)
            if row is None:
                nested.dropped = True
                continue
            scope = _inbound_scope(raw.get("IpRange"))
            if scope:
                row["Scope"] = scope
            kept.append(row)
        projected["InstanceInboundPermissions"] = kept
        if len(permissions) > MAX_INBOUND_PERMISSIONS:
            nested.capped = True
    elif permissions is not None:
        nested.dropped = True
    return _apply_nested({"status": STATUS_OK, "ContainerFleet": projected}, nested)


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
