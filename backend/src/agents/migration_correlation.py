"""Deterministic Agones-to-GameLift correlation over owned projections (#529).

Input is exactly two code-owned envelopes: the ``get_agones_inventory``
envelope (``agones_discovery``) and the ``list_gamelift_fleets`` envelope
(``gamelift_projections``, #464). Both are re-validated here, so a reshaped or
model-relayed copy cannot introduce new fields.

Rules:

* A mapping exists only on an exact, case-sensitive identifier match:
  an Agones Fleet name equal to a GameLift container fleet's
  ``GameServerContainerGroupDefinitionName``, or to a classic fleet's ``Name``.
* Agones Fleets are keyed by namespace and name, so ``team-a/game`` and
  ``team-b/game`` are two fleets.
* Exactly one candidate is ``matched``, but only when no other Agones Fleet
  claims that same GameLift fleet. A candidate claimed by two or more Agones
  Fleets makes every claiming row ``ambiguous`` with ``sharedCandidate`` and no
  target: a name match cannot tell which Agones Fleet it migrated from.
* Two or more candidates is ``ambiguous`` and names no target. Zero is
  ``unmatched`` only when both inventories are complete; otherwise it is
  ``undetermined``, because the missing row may be unread.
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

# Applied with ``fullmatch`` so a trailing newline ("games\n") is rejected.
_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,126}[a-z0-9])?$")
_NAMESPACE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_AGONES_COMPLETE = frozenset({"complete", "empty"})
_GAMELIFT_COMPLETE = frozenset({"ok", "empty"})


Candidate = tuple[int, dict[str, Any]]


def _gamelift_candidates(fleet_list: Mapping[str, Any]) -> dict[str, list[Candidate]]:
    """Index validated GameLift rows by their exact correlation key.

    Each candidate carries a code-owned position so two GameLift rows with the
    same projected fields are still counted as two candidates.
    """
    index: dict[str, list[Candidate]] = {}
    position = 0
    containers = fleet_list.get("ContainerFleets")
    for row in containers if isinstance(containers, list) else []:
        if not isinstance(row, Mapping):
            continue
        key = row.get("GameServerContainerGroupDefinitionName")
        if _valid_typed_string(key, _NAME):
            candidate: dict[str, Any] = {"FleetType": "container"}
            # The container fleet ID from the #464 row, so a match names the
            # fleet the per-fleet tools take, not only its definition.
            fleet_id = row.get("FleetId")
            if _valid_fleet_id(fleet_id) and fleet_id.startswith("containerfleet-"):
                candidate["FleetId"] = fleet_id
            candidate["GameServerContainerGroupDefinitionName"] = key
            candidate["basis"] = "container_group_definition_name"
            index.setdefault(key, []).append((position, candidate))
            position += 1
    classics = fleet_list.get("ClassicFleets")
    for row in classics if isinstance(classics, list) else []:
        if not isinstance(row, Mapping):
            continue
        key, fleet_id = row.get("Name"), row.get("FleetId")
        if _valid_typed_string(key, _NAME) and _valid_fleet_id(fleet_id):
            index.setdefault(key, []).append(
                (position, {"FleetType": "classic", "FleetId": fleet_id, "basis": "fleet_name"})
            )
            position += 1
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

    # First pass: identify each Agones Fleet by (namespace, name) and collect
    # its candidates, counting how many Agones Fleets claim each candidate.
    identified: list[tuple[str, str, list[Candidate]]] = []
    unidentified = 0
    claims: dict[int, int] = {}
    for fleet in raw_fleets:
        fleet = fleet if isinstance(fleet, Mapping) else {}
        name = fleet.get("name")
        namespace = fleet.get("namespace")
        namespace = namespace if isinstance(namespace, str) and _NAMESPACE.fullmatch(namespace) else ""
        if not isinstance(name, str) or not _K8S_NAME.fullmatch(name):
            unidentified += 1
            continue
        candidates = index.get(name, [])
        for position, _ in candidates:
            claims[position] = claims.get(position, 0) + 1
        identified.append((namespace, name, candidates))

    rows: list[tuple[str, str, dict[str, Any]]] = [("", "", {"match": UNIDENTIFIED}) for _ in range(unidentified)]
    for namespace, name, candidates in identified:
        row: dict[str, Any] = {"agonesFleet": name, "candidateCount": len(candidates)}
        if namespace:
            row["namespace"] = namespace
        shared = any(claims[position] > 1 for position, _ in candidates)
        if len(candidates) == 1 and not shared:
            row["match"] = MATCHED
            row["gameLift"] = dict(candidates[0][1])
        elif candidates:
            row["match"] = AMBIGUOUS
            if shared:
                row["sharedCandidate"] = True
        else:
            row["match"] = UNMATCHED if complete else UNDETERMINED
        rows.append((namespace, name, row))

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
