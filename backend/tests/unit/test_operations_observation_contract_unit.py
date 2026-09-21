"""Contract tests for the additive GameLift observation schema (issue #413)."""

from __future__ import annotations

# Standard library
from copy import deepcopy
from pathlib import Path
from typing import Any

# Third-party packages
import pytest
from jsonschema import Draft202012Validator

# Local modules
from operations.contracts import SCHEMA_NAMES, canonical_sha256, load_json, validate_playbook_binding
from operations.contracts.observation import (
    OBSERVATION_SCHEMA_NAME,
    ObservationContractError,
    load_observation_schema,
    validate_observation,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

SENSITIVE_FIELDS = {
    "access_token",
    "account_id",
    "arn",
    "display_name",
    "email",
    "provider_response",
    "provider_token",
    "refresh_token",
    "secret",
}


def _valid() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-observation.valid.json")


def test_observation_schema_is_valid_draft_2020_12() -> None:
    schema = load_observation_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "urn:game-agent:operations:contracts:v1:gamelift-observation"


def test_observation_schema_is_not_in_the_write_contract_set() -> None:
    # Additive: the observation schema must never join the published write-contract
    # set, whose hashes the source-control playbook binds.
    assert OBSERVATION_SCHEMA_NAME not in SCHEMA_NAMES


def test_write_contract_playbook_binding_is_unchanged_by_observation() -> None:
    prepared = load_json(FIXTURES / "source-control-prepared-operation.valid.json")
    playbook = load_json(FIXTURES / "source-control-playbook.valid.json")
    # The observation schema exists on disk; the write-contract playbook must
    # still validate and bind exactly the same schema set.
    validate_playbook_binding(prepared, playbook)


def test_valid_observation_passes() -> None:
    validate_observation(_valid())


def test_valid_observation_is_canonicalizable_and_hashes_stably() -> None:
    document = _valid()
    first = canonical_sha256(document)
    second = canonical_sha256(deepcopy(document))
    assert first == second
    assert first.startswith("sha256:")


def test_effective_authority_must_be_lowest_input() -> None:
    document = _valid()
    document["effective_authority"] = "advise"
    with pytest.raises(ObservationContractError) as exc:
        validate_observation(document)
    assert any("lowest authority input" in error for error in exc.value.errors)


def test_effective_authority_cannot_exceed_observe_ceiling() -> None:
    document = _valid()
    for field in document["authority_inputs"]:
        document["authority_inputs"][field] = "remediate"
    document["effective_authority"] = "remediate"
    with pytest.raises(ObservationContractError) as exc:
        validate_observation(document)
    assert any("observe phase ceiling" in error for error in exc.value.errors)


def test_disabled_deployment_cannot_produce_observation() -> None:
    document = _valid()
    document["authority_inputs"]["deployment_mode"] = "disabled"
    document["effective_authority"] = "disabled"
    with pytest.raises(ObservationContractError) as exc:
        validate_observation(document)
    assert any("disabled deployment" in error for error in exc.value.errors)


def test_capacity_minimum_cannot_exceed_maximum() -> None:
    document = _valid()
    document["results"]["capacity"][0]["minimum"] = 30
    document["results"]["capacity"][0]["maximum"] = 20
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_capacity_desired_must_be_within_range() -> None:
    document = _valid()
    document["results"]["capacity"][0]["desired"] = 999
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_duplicate_capacity_location_is_rejected() -> None:
    document = _valid()
    document["results"]["capacity"][1]["location"] = document["results"]["capacity"][0]["location"]
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_player_sessions_cannot_exceed_maximum() -> None:
    document = _valid()
    document["results"]["utilization"]["current_player_sessions"] = 1000
    document["results"]["utilization"]["maximum_player_sessions"] = 100
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_expiry_must_be_after_observed_at() -> None:
    document = _valid()
    document["expires_at"] = document["observed_at"]
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_additional_properties_are_rejected() -> None:
    document = _valid()
    document["provider_response"] = {"raw": "data"}
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_unbounded_scaling_policies_are_rejected() -> None:
    document = _valid()
    document["results"]["scaling_policies"] = [
        {"name": f"policy-{index}", "status": "ACTIVE", "metric_name": "PercentAvailableGameSessions"}
        for index in range(51)
    ]
    with pytest.raises(ObservationContractError):
        validate_observation(document)


def test_observation_carries_no_sensitive_fields() -> None:
    document = _valid()

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert key not in SENSITIVE_FIELDS, f"sensitive field leaked: {key}"
                _walk(child)
        elif isinstance(value, list):
            for child in value:
                _walk(child)

    _walk(document)
