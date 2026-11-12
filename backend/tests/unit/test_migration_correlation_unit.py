"""Unit tests for deterministic Agones-to-GameLift correlation (#529)."""

# Third-party packages
import pytest

# Local modules
from agents.agones_discovery import AGONES_ENVELOPE_VERSION
from agents.migration_correlation import (
    AMBIGUOUS,
    MATCHED,
    MAX_CORRELATED_FLEETS,
    UNDETERMINED,
    UNIDENTIFIED,
    UNMATCHED,
    correlate,
)

pytestmark = pytest.mark.unit

FLEET = "fleet-0a1b2c3d-1111-2222-3333-4444aaaabbbb"


def _agones(*names, outcome="complete", has_more=False):
    section = {"outcome": outcome, "items": [{"name": n, "namespace": "games"} if n else {} for n in names]}
    if has_more:
        section["hasMore"] = True
    return {"v": AGONES_ENVELOPE_VERSION, "fleets": section}


def _gamelift(container_names=(), classic=(), status="ok", **flags):
    return {
        "status": status,
        "ContainerFleets": [
            {"FleetType": "container", "GameServerContainerGroupDefinitionName": n} for n in container_names
        ],
        "ClassicFleets": [{"FleetId": fid, "Name": name} for fid, name in classic],
        **flags,
    }


def test_exact_container_match():
    result = correlate(_agones("echo"), _gamelift(container_names=["echo"]))
    assert result["outcome"] == "complete"
    assert result["mappings"] == [
        {
            "agonesFleet": "echo",
            "candidateCount": 1,
            "namespace": "games",
            "match": MATCHED,
            "gameLift": {
                "FleetType": "container",
                "GameServerContainerGroupDefinitionName": "echo",
                "basis": "container_group_definition_name",
            },
        }
    ]


def test_exact_classic_match():
    result = correlate(_agones("echo"), _gamelift(classic=[(FLEET, "echo")]))
    assert result["mappings"][0]["gameLift"] == {"FleetType": "classic", "FleetId": FLEET, "basis": "fleet_name"}


@pytest.mark.parametrize("near", ["Echo", "echo-v2", "echo ", "ech", "echo-server"])
def test_no_fuzzy_prefix_or_case_mapping(near):
    result = correlate(_agones("echo"), _gamelift(container_names=[near]))
    row = result["mappings"][0]
    assert row["match"] == UNMATCHED and "gameLift" not in row


def test_ambiguous_names_no_target():
    result = correlate(_agones("echo"), _gamelift(container_names=["echo"], classic=[(FLEET, "echo")]))
    row = result["mappings"][0]
    assert row["match"] == AMBIGUOUS and row["candidateCount"] == 2 and "gameLift" not in row


def test_missing_identifier_is_unidentified():
    result = correlate(_agones(None), _gamelift(container_names=["echo"]))
    assert result["mappings"] == [{"match": UNIDENTIFIED}]


@pytest.mark.parametrize(
    "agones,gamelift",
    [
        (_agones("echo", outcome="partial"), _gamelift()),
        (_agones("echo", has_more=True, outcome="truncated"), _gamelift()),
        (_agones("echo"), _gamelift(status="incomplete")),
        (_agones("echo"), _gamelift(truncated=True)),
        (_agones("echo"), _gamelift(status="denied")),
    ],
)
def test_incomplete_inventory_never_claims_unmatched(agones, gamelift):
    result = correlate(agones, gamelift)
    assert result["outcome"] == "partial" and result["inventoriesComplete"] is False
    assert result["mappings"][0]["match"] == UNDETERMINED


def test_deterministic_order_and_cap():
    names = [f"f{i:02d}" for i in range(MAX_CORRELATED_FLEETS + 3)]
    a = correlate(_agones(*reversed(names)), _gamelift())
    b = correlate(_agones(*names), _gamelift())
    assert a == b
    assert [m["agonesFleet"] for m in a["mappings"]] == names[:MAX_CORRELATED_FLEETS]
    assert a["truncated"] is True


@pytest.mark.parametrize("bad", [None, {}, {"v": "other"}, "text"])
def test_malformed_inputs(bad):
    assert correlate(bad, _gamelift())["outcome"] == "malformed"
    assert correlate(_agones("echo"), bad)["outcome"] == "malformed"


def test_invalid_gamelift_values_are_ignored():
    gamelift = _gamelift(classic=[("arn:aws:gamelift:us-west-2:000000000000:fleet/x", "echo")])
    result = correlate(_agones("echo"), gamelift)
    assert result["mappings"][0]["match"] == UNMATCHED
