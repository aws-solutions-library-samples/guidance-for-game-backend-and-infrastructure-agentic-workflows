"""Concrete DynamoDB autonomy policy loader + seed API (issue #439, #440 deploy).

The evaluator must load the EXACT server-owned policy by configured id, version,
and hash from a durable store — never from the event or model. The loader
re-validates the loaded policy's hash against the configured hash and against the
frozen #438 contract, failing closed on any drift. The seed API (used by the
issue #440 deploy wrapper) persists a policy with conditional immutability and
refuses to persist a policy whose declared hash does not match its bytes.
"""

from __future__ import annotations

# Standard library
import json
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime.evaluator_entry import (
    AutonomyPolicyLoaderError,
    DynamoDbAutonomyPolicyLoader,
)
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


class _ConditionalFailure(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, *, TableName: str, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in self.items:
            raise _ConditionalFailure()
        self.items[key] = Item

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item is not None else {}


@pytest.mark.unit
def test_seed_then_load_round_trips_the_exact_policy() -> None:
    policy = _policy()
    loader = DynamoDbAutonomyPolicyLoader(client=_FakeDynamo(), table_name="ops-06")
    loader.seed(policy)
    loaded = loader.load(
        policy_id=policy["policy_id"],
        policy_version=policy["policy_version"],
        policy_hash=policy["policy_hash"],
    )
    assert loaded == policy


@pytest.mark.unit
def test_load_refuses_when_configured_hash_mismatches() -> None:
    policy = _policy()
    loader = DynamoDbAutonomyPolicyLoader(client=_FakeDynamo(), table_name="ops-06")
    loader.seed(policy)
    with pytest.raises(AutonomyPolicyLoaderError):
        loader.load(
            policy_id=policy["policy_id"],
            policy_version=policy["policy_version"],
            policy_hash="sha256:" + "0" * 64,
        )


@pytest.mark.unit
def test_load_refuses_when_policy_absent() -> None:
    loader = DynamoDbAutonomyPolicyLoader(client=_FakeDynamo(), table_name="ops-06")
    with pytest.raises(AutonomyPolicyLoaderError):
        loader.load(policy_id="policy.missing", policy_version="1", policy_hash="sha256:" + "0" * 64)


@pytest.mark.unit
def test_seed_refuses_policy_with_mismatched_declared_hash() -> None:
    policy = _policy()
    tampered = dict(policy, policy_hash="sha256:" + "1" * 64)
    loader = DynamoDbAutonomyPolicyLoader(client=_FakeDynamo(), table_name="ops-06")
    with pytest.raises(AutonomyPolicyLoaderError):
        loader.seed(tampered)


@pytest.mark.unit
def test_seed_is_conditionally_immutable() -> None:
    policy = _policy()
    loader = DynamoDbAutonomyPolicyLoader(client=_FakeDynamo(), table_name="ops-06")
    loader.seed(policy)
    # An identical re-seed is idempotent.
    loader.seed(policy)
