"""Unit tests for the E0 measurement harness document assembly (#412).

These tests exercise the harness against an in-process fake GameLift client so
no live AWS call is made. They verify:

* the three reads issued are exactly the bounded read-only fleet describes;
* the emitted evidence document is public-safe (no account id, fleet id, or ARN);
* the acceptance evaluation uses ``p99 <= ceiling - cancellation_margin``; and
* failures and partial results are counted, never reported as success.
"""

from __future__ import annotations

# Standard library
import json

# Third-party packages
import pytest

# Local modules
from operations.validation.e0_harness import _measure, _RunConfig, _short_target_ref
from operations.validation.e0_latency import DEFAULT_BUDGET

pytestmark = pytest.mark.unit

# A synthetic, clearly-fake fleet id (public-content rules: fixed test value).
FAKE_FLEET_ID = "fleet-00000000-0000-4000-8000-000000000000"
FAKE_ACCOUNT_ID = "123456789012"


class FakeGameLiftClient:
    """Records the read calls it receives and returns synthetic responses."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    def describe_fleet_utilization(self, **kwargs):
        self.calls.append("describe_fleet_utilization")
        if self._fail:
            raise RuntimeError("synthetic provider failure")
        return {"FleetUtilization": [{"ActiveGameSessionCount": 0}]}

    def describe_fleet_capacity(self, **kwargs):
        self.calls.append("describe_fleet_capacity")
        return {"FleetCapacity": [{"InstanceCounts": {"ACTIVE": 1}}]}

    def describe_scaling_policies(self, **kwargs):
        self.calls.append("describe_scaling_policies")
        return {"ScalingPolicies": []}

    def list_fleets(self, **kwargs):
        return {"FleetIds": []}


def _config(samples: int = 5, concurrency: int = 1) -> _RunConfig:
    return _RunConfig(
        region="us-west-2",
        samples=samples,
        concurrency=concurrency,
        retry_mode="adaptive",
        max_attempts=3,
    )


def test_measure_issues_exactly_three_bounded_reads_per_sample():
    client = FakeGameLiftClient()
    _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=2))
    # 2 samples * 3 reads each.
    assert client.calls.count("describe_fleet_utilization") == 2
    assert client.calls.count("describe_fleet_capacity") == 2
    assert client.calls.count("describe_scaling_policies") == 2
    # No other (write) call was made.
    assert set(client.calls) == {
        "describe_fleet_utilization",
        "describe_fleet_capacity",
        "describe_scaling_policies",
    }


def test_measure_document_is_public_safe():
    client = FakeGameLiftClient()
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=3))
    text = json.dumps(doc)
    # Never leaks the fleet id or an account id.
    assert FAKE_FLEET_ID not in text
    assert FAKE_ACCOUNT_ID not in text
    assert "arn:aws" not in text
    # Target is referenced by a stable non-reversible short hash.
    assert doc["target_ref"] == _short_target_ref(FAKE_FLEET_ID)
    assert doc["target_ref"].startswith("fleet-")


def test_measure_records_required_statistics_and_assumptions():
    client = FakeGameLiftClient()
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=10))
    results = doc["results_ms"]
    for key in ("sample_size", "p50_ms", "p95_ms", "p99_ms", "max_ms", "failures", "timeouts"):
        assert key in results
    assert results["sample_size"] == 10
    assert results["percentile_method"] == "nearest_rank"
    a = doc["assumptions"]
    for key in ("region", "sample_size", "concurrency", "boto3_retry_mode", "provider_reads"):
        assert key in a
    assert a["provider_reads"] == [
        "describe_fleet_utilization",
        "describe_fleet_capacity",
        "describe_scaling_policies",
    ]


def test_measure_evaluation_uses_ceiling_minus_margin():
    client = FakeGameLiftClient()
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=5))
    ev = doc["evaluation"]
    expected = (DEFAULT_BUDGET.ceiling_s - DEFAULT_BUDGET.cancellation_margin_s) * 1000.0
    assert ev["acceptance_ceiling_ms"] == pytest.approx(expected)
    # The default (in-memory, test-only) persistence mode is NOT acceptable as
    # live evidence, so even a fast clean run is denied (issue #412 finding).
    assert ev["persistence_acceptable"] is False
    assert ev["synchronous_accepted"] is False


def test_measure_accepts_only_under_real_transactional_persistence():
    # Third-party packages / local: a fake DynamoDB client keeps this offline.
    # Local modules
    from operations.validation.e0_persistence import DynamoDbTransactionalSink

    class FakeDynamoDbClient:
        def __init__(self):
            self.calls = []

        def transact_write_items(self, **kwargs):
            self.calls.append(kwargs)
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    client = FakeGameLiftClient()
    sink = DynamoDbTransactionalSink(
        client=FakeDynamoDbClient(),
        table_name="e0-latency-spike-disposable",
        persistence_budget_s=DEFAULT_BUDGET.persistence_s,
    )
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=5), sink=sink)
    ev = doc["evaluation"]
    # Fast fakes under the real transactional mode pass the synchronous gate.
    assert ev["persistence_acceptable"] is True
    assert ev["clean_run"] is True
    assert ev["synchronous_accepted"] is True


def test_measure_counts_provider_failures_and_denies_success():
    client = FakeGameLiftClient(fail=True)
    doc = _measure(client, FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=4))
    assert doc["results_ms"]["failures"] == 4
    assert doc["results_ms"]["successes"] == 0
    assert doc["evaluation"]["synchronous_accepted"] is False


def test_short_target_ref_is_deterministic_and_non_reversible():
    ref1 = _short_target_ref(FAKE_FLEET_ID)
    ref2 = _short_target_ref(FAKE_FLEET_ID)
    assert ref1 == ref2
    assert FAKE_FLEET_ID not in ref1
