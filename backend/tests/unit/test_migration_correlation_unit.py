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


def _agones(*names, outcome="complete", has_more=False, namespace="games"):
    section = {"outcome": outcome, "items": [{"name": n, "namespace": namespace} if n else {} for n in names]}
    if has_more:
        section["hasMore"] = True
    return {"v": AGONES_ENVELOPE_VERSION, "fleets": section}


def _gamelift(container_names=(), classic=(), status="ok", container_ids=None, **flags):
    ids = list(container_ids) if container_ids is not None else [None] * len(container_names)
    return {
        "status": status,
        "ContainerFleets": [
            {"FleetType": "container", "GameServerContainerGroupDefinitionName": n}
            | ({"FleetId": fid} if fid is not None else {})
            for n, fid in zip(container_names, ids)
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


def _agones_rows(*rows, outcome="complete"):
    return {
        "v": AGONES_ENVELOPE_VERSION,
        "fleets": {"outcome": outcome, "items": [{"namespace": ns, "name": n} for ns, n in rows]},
    }


@pytest.mark.parametrize(
    "gamelift",
    [
        _gamelift(container_names=["game"]),
        _gamelift(classic=[(FLEET, "game")]),
    ],
)
def test_same_name_in_two_namespaces_is_ambiguous_not_many_to_one(gamelift):
    """team-a/game and team-b/game must not both map to one GameLift fleet."""
    result = correlate(_agones_rows(("team-a", "game"), ("team-b", "game")), gamelift)

    assert result["outcome"] == "complete"
    assert [(m["namespace"], m["agonesFleet"]) for m in result["mappings"]] == [
        ("team-a", "game"),
        ("team-b", "game"),
    ]
    for row in result["mappings"]:
        assert row["match"] == AMBIGUOUS
        assert row["candidateCount"] == 1
        assert row["sharedCandidate"] is True
        assert "gameLift" not in row


def test_same_name_in_two_namespaces_with_two_candidates_names_no_target():
    gamelift = _gamelift(container_names=["game", "game"])
    result = correlate(_agones_rows(("team-a", "game"), ("team-b", "game")), gamelift)

    assert {m["match"] for m in result["mappings"]} == {AMBIGUOUS}
    assert all("gameLift" not in m for m in result["mappings"])


def test_duplicate_agones_rows_never_match():
    result = correlate(_agones_rows(("games", "echo"), ("games", "echo")), _gamelift(container_names=["echo"]))

    assert [m["match"] for m in result["mappings"]] == [AMBIGUOUS, AMBIGUOUS]


def test_unshared_candidate_still_matches_next_to_a_shared_one():
    gamelift = _gamelift(container_names=["game", "lobby"])
    result = correlate(_agones_rows(("team-a", "game"), ("team-b", "game"), ("team-a", "lobby")), gamelift)

    by_key = {(m["namespace"], m["agonesFleet"]): m for m in result["mappings"]}
    assert by_key[("team-a", "lobby")]["match"] == MATCHED
    assert by_key[("team-a", "game")]["match"] == AMBIGUOUS
    assert by_key[("team-b", "game")]["match"] == AMBIGUOUS


CFLEET = "containerfleet-0a1b2c3d-1111-2222-3333-4444aaaabbbb"


def test_container_match_names_the_container_fleet_id():
    result = correlate(_agones("echo"), _gamelift(container_names=["echo"], container_ids=[CFLEET]))

    assert result["mappings"][0]["gameLift"] == {
        "FleetType": "container",
        "FleetId": CFLEET,
        "GameServerContainerGroupDefinitionName": "echo",
        "basis": "container_group_definition_name",
    }


@pytest.mark.parametrize(
    "bad_id",
    [FLEET, f"arn:aws:gamelift:us-west-2:000000000000:containerfleet/{CFLEET}", "containerfleet-x\n", 7],
)
def test_container_match_drops_an_invalid_or_classic_fleet_id(bad_id):
    result = correlate(_agones("echo"), _gamelift(container_names=["echo"], container_ids=[bad_id]))

    row = result["mappings"][0]
    assert row["match"] == MATCHED
    assert "FleetId" not in row["gameLift"]
