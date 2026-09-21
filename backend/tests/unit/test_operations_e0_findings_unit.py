"""Regression tests for confirmed E0 review findings (#412).

Each test pins one behavior a reviewer flagged on the original spike:

* synchronous acceptance must fail when ANY sample failed, timed out, or was a
  partial denial — not merely when a positive-p99 subset exists;
* partial denials must be reported separately from timeouts;
* classic-fleet discovery must distinguish classic (EC2) from container fleets
  and page through all fleets, not treat any ``list_fleets`` id as classic;
* the concurrency arrival model must be declared in the evidence document so a
  measured p99 has a defensible meaning.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.validation.e0_harness import (
    _classic_fleet_ids,
    _measure,
    _RunConfig,
)
from operations.validation.e0_latency import DEFAULT_BUDGET

pytestmark = pytest.mark.unit

FAKE_FLEET_ID = "fleet-00000000-0000-4000-8000-000000000000"


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kwargs):
        yield from self._pages


class FakeGameLiftClient:
    """Fake supporting paginated fleet discovery and the three reads."""

    def __init__(self, *, fail=False, partial=False, list_fleets_pages=None, attributes=None, container_ids=None):
        self.calls: list[str] = []
        self._fail = fail
        self._partial = partial
        self._list_fleets_pages = list_fleets_pages or [{"FleetIds": []}]
        self._attributes = attributes or []
        self._container_ids = container_ids or []

    def get_paginator(self, operation_name):
        if operation_name == "list_fleets":
            return _FakePaginator(self._list_fleets_pages)
        if operation_name == "list_container_fleets":
            return _FakePaginator([{"ContainerFleets": [{"FleetId": fid} for fid in self._container_ids]}])
        raise AssertionError(f"unexpected paginator: {operation_name}")

    def describe_fleet_attributes(self, FleetIds):  # noqa: N803 - boto3 kwarg name
        return {"FleetAttributes": [a for a in self._attributes if a["FleetId"] in set(FleetIds)]}

    def describe_fleet_utilization(self, **_kwargs):
        self.calls.append("describe_fleet_utilization")
        if self._fail:
            raise RuntimeError("synthetic provider failure")
        if self._partial:
            return None
        return {"FleetUtilization": []}

    def describe_fleet_capacity(self, **_kwargs):
        self.calls.append("describe_fleet_capacity")
        return {"FleetCapacity": []}

    def describe_scaling_policies(self, **_kwargs):
        self.calls.append("describe_scaling_policies")
        return {"ScalingPolicies": []}


def _config(samples=5, concurrency=1):
    return _RunConfig(
        region="us-west-2",
        samples=samples,
        concurrency=concurrency,
        retry_mode="adaptive",
        max_attempts=3,
        arrival_model="closed-loop",
    )


# ---------------------------------------------------------------------------
# Finding: acceptance must be strict about failures/timeouts/partial denials
# ---------------------------------------------------------------------------


def test_any_provider_failure_denies_synchronous_acceptance():
    client = FakeGameLiftClient(fail=True)
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=4))
    assert doc["results_ms"]["failures"] == 4
    assert doc["evaluation"]["synchronous_accepted"] is False


def test_partial_denial_reported_separately_and_denies_acceptance():
    client = FakeGameLiftClient(partial=True)
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=3))
    results = doc["results_ms"]
    # Partial denials are their own bucket, not folded into timeouts.
    assert results["partial_denials"] == 3
    assert results["timeouts"] == 0
    assert results["successes"] == 0
    assert doc["evaluation"]["synchronous_accepted"] is False


def test_acceptance_requires_zero_failed_samples_even_with_good_p99():
    """Even one non-success denies acceptance regardless of the success p99."""

    # 4 clean successes plus one failure: p99 over successes would pass, but the
    # presence of any non-success must deny.
    class MostlyOkClient(FakeGameLiftClient):
        def __init__(self):
            super().__init__()
            self._n = 0

        def describe_fleet_utilization(self, **_kwargs):
            self.calls.append("describe_fleet_utilization")
            self._n += 1
            if self._n == 1:
                raise RuntimeError("one bad sample")
            return {"FleetUtilization": []}

    doc = _measure(MostlyOkClient(), FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=5, concurrency=1))
    assert doc["results_ms"]["failures"] >= 1
    assert doc["evaluation"]["synchronous_accepted"] is False


class _FakeDynamoDbClient:
    """Records transactional writes; issues no I/O (offline test double)."""

    def __init__(self):
        self.calls: list[dict] = []

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def test_all_clean_successes_accept_under_real_transactional_persistence():
    # Local modules
    from operations.validation.e0_persistence import DynamoDbTransactionalSink

    client = FakeGameLiftClient()
    sink = DynamoDbTransactionalSink(
        client=_FakeDynamoDbClient(),
        table_name="e0-latency-spike-disposable",
        persistence_budget_s=DEFAULT_BUDGET.persistence_s,
    )
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=5), sink=sink)
    assert doc["results_ms"]["failures"] == 0
    assert doc["results_ms"]["timeouts"] == 0
    assert doc["results_ms"]["partial_denials"] == 0
    assert doc["evaluation"]["persistence_acceptable"] is True
    assert doc["evaluation"]["synchronous_accepted"] is True


def test_clean_run_under_in_memory_mode_is_denied():
    # A clean sample is NOT sufficient: the in-memory (test-only) mode is never
    # acceptable as live evidence, so acceptance is denied (issue #412 finding).
    client = FakeGameLiftClient()
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=5))
    assert doc["evaluation"]["clean_run"] is True
    assert doc["evaluation"]["persistence_acceptable"] is False
    assert doc["evaluation"]["synchronous_accepted"] is False


# ---------------------------------------------------------------------------
# Finding: arrival model must be declared in the evidence
# ---------------------------------------------------------------------------


def test_evidence_declares_arrival_model():
    client = FakeGameLiftClient()
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=3, concurrency=2))
    assert doc["assumptions"]["arrival_model"] == "closed-loop"
    # Evaluation records what the acceptance verdict was based on.
    assert "clean_run" in doc["evaluation"]


# ---------------------------------------------------------------------------
# Finding: classic vs container fleet discovery, paginated
# ---------------------------------------------------------------------------


def test_classic_fleet_ids_excludes_container_and_paginates():
    client = FakeGameLiftClient(
        list_fleets_pages=[
            {"FleetIds": ["fleet-classic-1", "fleet-container-1"]},
            {"FleetIds": ["fleet-classic-2"]},
        ],
        attributes=[
            {"FleetId": "fleet-classic-1", "ComputeType": "EC2"},
            {"FleetId": "fleet-classic-2", "ComputeType": "EC2"},
            {"FleetId": "fleet-container-1", "ComputeType": "CONTAINER"},
        ],
        container_ids=["fleet-container-1"],
    )
    classic = _classic_fleet_ids(client)
    assert set(classic) == {"fleet-classic-1", "fleet-classic-2"}


def test_classic_fleet_ids_empty_when_only_container():
    client = FakeGameLiftClient(
        list_fleets_pages=[{"FleetIds": ["fleet-container-1"]}],
        attributes=[{"FleetId": "fleet-container-1", "ComputeType": "CONTAINER"}],
        container_ids=["fleet-container-1"],
    )
    assert _classic_fleet_ids(client) == []
