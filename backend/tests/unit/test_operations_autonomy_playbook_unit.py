"""Autonomy playbook hash tests for the additive E5 v2 layer (issue #438).

The bounded-autonomy playbook is a distinct v2 definition
(``playbook.gamelift-capacity-autonomy`` / ``2.0.0``). Its hash MUST be pinned,
MUST be distinct from the E2 ``playbook.gamelift-capacity`` / ``1.0.0`` hash, and
MUST change if any bound field drifts. The executor identity/binding is
deliberately preserved from E2/E3.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy

# Third-party packages
import pytest

# Local modules
from operations.autonomy_playbook_definition import (
    AUTONOMY_PLAYBOOK_DEFINITION,
    EXECUTOR_BINDING_VERSION,
    EXECUTOR_ID,
    PLAYBOOK_ID,
    PLAYBOOK_VERSION,
    autonomy_playbook_hash,
)
from operations.contracts import canonical_sha256, load_schema
from operations.contracts.autonomy import AUTONOMY_SCHEMA_NAMES, load_autonomy_schema
from operations.contracts.observation import load_observation_schema
from operations.playbook_definition import EXECUTOR_ID as V1_EXECUTOR_ID
from operations.playbook_definition import capacity_playbook_hash

_EXPECTED_AUTONOMY_PLAYBOOK_HASH = "sha256:29b3047e98446ce4f919610418fd28ecba7339dfdb28382846913749f283b52f"


@pytest.mark.unit
def test_autonomy_playbook_hash_is_pinned() -> None:
    assert autonomy_playbook_hash() == canonical_sha256(AUTONOMY_PLAYBOOK_DEFINITION)
    assert autonomy_playbook_hash() == _EXPECTED_AUTONOMY_PLAYBOOK_HASH


@pytest.mark.unit
def test_autonomy_playbook_binds_every_transitive_schema_hash() -> None:
    schemas = [load_schema("common"), load_observation_schema()]
    schemas.extend(load_autonomy_schema(name) for name in sorted(AUTONOMY_SCHEMA_NAMES))
    expected = {schema["$id"]: canonical_sha256(schema) for schema in schemas}
    actual = {binding["schema_id"]: binding["schema_hash"] for binding in AUTONOMY_PLAYBOOK_DEFINITION["schemas"]}

    assert actual == expected
    assert len(actual) == len(AUTONOMY_PLAYBOOK_DEFINITION["schemas"])


@pytest.mark.unit
def test_any_transitive_schema_drift_changes_the_playbook_hash() -> None:
    drifted = deepcopy(AUTONOMY_PLAYBOOK_DEFINITION)
    drifted["schemas"][0]["schema_hash"] = "sha256:" + "f" * 64
    assert canonical_sha256(drifted) != autonomy_playbook_hash()


@pytest.mark.unit
def test_autonomy_playbook_hash_is_distinct_from_v1() -> None:
    assert autonomy_playbook_hash() != capacity_playbook_hash()


@pytest.mark.unit
def test_autonomy_playbook_preserves_the_v1_executor_identity() -> None:
    # The single UpdateFleetCapacity executor is reused unchanged; only the
    # precondition set and execution authority differ.
    assert EXECUTOR_ID == V1_EXECUTOR_ID == "executor.gamelift-capacity"
    assert EXECUTOR_BINDING_VERSION == "1.0"
    assert AUTONOMY_PLAYBOOK_DEFINITION["executor_binding"] == {
        "executor_id": EXECUTOR_ID,
        "executor_binding_version": EXECUTOR_BINDING_VERSION,
    }


@pytest.mark.unit
def test_autonomy_playbook_carries_the_complete_immutable_binding() -> None:
    definition = AUTONOMY_PLAYBOOK_DEFINITION
    assert definition["playbook_id"] == PLAYBOOK_ID == "playbook.gamelift-capacity-autonomy"
    assert definition["playbook_version"] == PLAYBOOK_VERSION == "2.0.0"
    assert definition["profile"] == "gamelift.capacity-adjustment/2.0"
    assert definition["capability"] == {
        "capability_id": "gamelift.capacity-adjustment",
        "capability_version": "2.0",
    }
    assert definition["capacity_bounds"] == {"floor": 0, "ceiling": 1, "max_step": 1}
    assert definition["required_execution_authority"] == "operate"
    # No human approval precondition; the guardrail preconditions are present.
    assert "APPROVAL_GRANTED" not in definition["preconditions"]
    assert "DECISION_UNEXPIRED" in definition["preconditions"]
    assert "WITHIN_CAPACITY_ENVELOPE" in definition["preconditions"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("playbook_version", "2.0.1"),
        lambda d: d["capability"].__setitem__("capability_version", "3.0"),
        lambda d: d["capacity_bounds"].__setitem__("ceiling", 2),
        lambda d: d["executor_binding"].__setitem__("executor_id", "executor.other"),
        lambda d: d.__setitem__("required_execution_authority", "remediate"),
        lambda d: d["preconditions"].append("APPROVAL_GRANTED"),
    ],
)
def test_any_playbook_drift_changes_the_hash(mutate) -> None:
    drifted = deepcopy(AUTONOMY_PLAYBOOK_DEFINITION)
    mutate(drifted)
    assert canonical_sha256(drifted) != autonomy_playbook_hash()
