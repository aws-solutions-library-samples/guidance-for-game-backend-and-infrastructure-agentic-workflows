"""Deterministic Agones-to-GameLift correlation over owned projections (#529).

Input is exactly two code-owned envelopes: the ``get_agones_inventory``
envelope (``agones_discovery``) and the ``list_gamelift_fleets`` envelope
(``gamelift_projections``, #464). Both are re-validated here, so a reshaped or
model-relayed copy cannot introduce new fields.

Rules:

* A mapping exists only on an exact, case-sensitive identifier match:
  an Agones Fleet name equal to a GameLift container fleet's
  ``GameServerContainerGroupDefinitionName``, or to a classic fleet's ``Name``.
* Exactly one candidate is ``matched``. Two or more is ``ambiguous`` and names
  no target. Zero is ``unmatched`` only when both inventories are complete;
  otherwise it is ``undetermined``, because the missing row may be unread.
* An Agones fleet without a valid name is ``unidentified``.
* Output order is sorted by namespace and name, so the result is stable.

No similarity, prefix, label, or model inference is used. The function has no
I/O and no authority: it reads only what the two read-only tools returned.
"""

from __future__ import annotations

# Standard library
import re
from typing import Any, Mapping

# Local modules
from agents.agones_discovery import AGONES_ENVELOPE_VERSION
from agents.gamelift_projections import _NAME, _valid_fleet_id, _valid_typed_string

CORRELATION_VERSION = "migration-correlation-1"
MAX_CORRELATED_FLEETS = 10

MATCHED = "matched"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"
UNDETERMINED = "undetermined"
UNIDENTIFIED = "unidentified"

_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,126}[a-z0-9])?$")
_NAMESPACE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_AGONES_COMPLETE = frozenset({"complete", "empty"})
_GAMELIFT_COMPLETE = frozenset({"ok", "empty"})


def _gamelift_candidates(fleet_list: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Index validated GameLift rows by their exact correlation key."""
    index: dict[str, list[dict[str, Any]]] = {}
    containers = fleet_list.get("ContainerFleets")
    for row in containers if isinstance(containers, list) else []:
        if not isinstance(row, Mapping):
            continue
        key = row.get("GameServerContainerGroupDefinitionName")
        if _valid_typed_string(key, _NAME):
            index.setdefault(key, []).append(
                {
                    "FleetType": "container",
                    "GameServerContainerGroupDefinitionName": key,
                    "basis": "container_group_definition_name",
                }
            )
    classics = fleet_list.get("ClassicFleets")
    for row in classics if isinstance(classics, list) else []:
        if not isinstance(row, Mapping):
            continue
        key, fleet_id = row.get("Name"), row.get("FleetId")
        if _valid_typed_string(key, _NAME) and _valid_fleet_id(fleet_id):
            index.setdefault(key, []).append({"FleetType": "classic", "FleetId": fleet_id, "basis": "fleet_name"})
    return index


def correlate(agones_inventory: Any, gamelift_fleet_list: Any) -> dict[str, Any]:
    """Correlate Agones Fleets with GameLift fleets. Never invents a mapping."""
    if (
        not isinstance(agones_inventory, Mapping)
        or agones_inventory.get("v") != AGONES_ENVELOPE_VERSION
        or not isinstance(gamelift_fleet_list, Mapping)
        or not isinstance(gamelift_fleet_list.get("status"), str)
    ):
        return {"v": CORRELATION_VERSION, "outcome": "malformed", "mappings": []}

    fleets_section = agones_inventory.get("fleets")
    fleets_section = fleets_section if isinstance(fleets_section, Mapping) else {}
    raw_fleets = fleets_section.get("items")
    raw_fleets = raw_fleets if isinstance(raw_fleets, list) else []

    complete = (
        fleets_section.get("outcome") in _AGONES_COMPLETE
        and not fleets_section.get("hasMore")
        and gamelift_fleet_list.get("status") in _GAMELIFT_COMPLETE
        and not gamelift_fleet_list.get("truncated")
        and not gamelift_fleet_list.get("partial")
    )
    index = _gamelift_candidates(gamelift_fleet_list)

    rows: list[tuple[str, str, dict[str, Any]]] = []
    for fleet in raw_fleets:
        fleet = fleet if isinstance(fleet, Mapping) else {}
        name = fleet.get("name")
        namespace = fleet.get("namespace")
        namespace = namespace if isinstance(namespace, str) and _NAMESPACE.match(namespace) else None
        if not isinstance(name, str) or not _K8S_NAME.match(name):
            rows.append(("", "", {"match": UNIDENTIFIED}))
            continue
        candidates = index.get(name, [])
        row: dict[str, Any] = {"agonesFleet": name, "candidateCount": len(candidates)}
        if namespace:
            row["namespace"] = namespace
        if len(candidates) == 1:
            row["match"] = MATCHED
            row["gameLift"] = dict(candidates[0])
        elif len(candidates) > 1:
            row["match"] = AMBIGUOUS
        else:
            row["match"] = UNMATCHED if complete else UNDETERMINED
        rows.append((namespace or "", name, row))

    rows.sort(key=lambda entry: (entry[0], entry[1]))
    mappings = [row for _, _, row in rows[:MAX_CORRELATED_FLEETS]]
    result: dict[str, Any] = {
        "v": CORRELATION_VERSION,
        "outcome": "complete" if complete else "partial",
        "inventoriesComplete": complete,
        "mappings": mappings,
    }
    if len(rows) > MAX_CORRELATED_FLEETS:
        result["truncated"] = True
    return result


__all__ = [
    "AMBIGUOUS",
    "CORRELATION_VERSION",
    "MATCHED",
    "MAX_CORRELATED_FLEETS",
    "UNDETERMINED",
    "UNIDENTIFIED",
    "UNMATCHED",
    "correlate",
]
