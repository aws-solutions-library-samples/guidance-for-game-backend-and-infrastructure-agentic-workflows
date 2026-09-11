"""Cross-worker reuse tests for the shared cost report snapshot store (#365).

These exercise the DynamoDB store the way two independent AgentCore workers do:
a shared fake table backs two *independently constructed* ``DynamoDbCostSnapshotStore``
instances, each wired to its own ``CostReportService`` with its own Cost Explorer
client. A report created through the "writer" service must resolve through the
"reader" service — deterministically, strongly consistent, scope-checked,
expiry-honoring, and without the reader ever calling Cost Explorer.

The fake reproduces the two DynamoDB behaviors this feature depends on:
* conditional ``PutItem`` with ``attribute_not_exists(reportId)`` prevents an
  overwrite of an immutable snapshot, and
* ``GetItem`` with ``ConsistentRead=True`` returns the just-written item.

Table-level integration against a *real* DynamoDB table is intentionally NOT run
here. The runtime IAM grants only GetItem/PutItem (no DeleteItem), so a test
could not clean up after itself, and TTL deletion is asynchronous — so a real
table cannot be exercised safely and self-cleaningly from a unit run. That
coverage is a pipeline/deployment verification item and is documented in the PR
rather than encoded as a permanently skipped placeholder test.
"""

# Standard library
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from agents.cost_report import CostReportError, CostReportService
from agents.cost_report_scope import ReportScope, reset_request_scope, set_request_scope
from agents.cost_snapshot_dynamodb import DynamoDbCostSnapshotStore
from agents.cost_snapshot_serialization import ATTR_REPORT_ID
from agents.cost_snapshot_store import SnapshotCollisionError, SnapshotRecord

pytestmark = pytest.mark.unit

_FIXED_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
_FIXED_EPOCH = int(_FIXED_NOW.timestamp())
_TRUSTED_SCOPE = ReportScope(tenant="tenant-1", workspace="ws-1", actor="alice")


class FakeDynamoDbTable:
    """A shared in-memory stand-in for one DynamoDB table."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def put_item(self, *, TableName, Item, ConditionExpression=None):  # noqa: N803 (boto3 casing)
        key = Item[ATTR_REPORT_ID]["S"]
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        # Store a copy so callers cannot mutate persisted state.
        self.items[key] = deepcopy(Item)
        return {}

    def get_item(self, *, TableName, Key, ConsistentRead=False):  # noqa: N803 (boto3 casing)
        key = Key[ATTR_REPORT_ID]["S"]
        item = self.items.get(key)
        return {"Item": deepcopy(item)} if item is not None else {}


def _ce_client() -> MagicMock:
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01", "End": "2026-05-16"},
                "Estimated": False,
                "Groups": [
                    {"Keys": ["Amazon EKS"], "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}}},
                    {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "20.00", "Unit": "USD"}}},
                ],
            }
        ]
    }
    return client


def _service_for(
    table: FakeDynamoDbTable, ce_client: MagicMock, *, report_id: str, now_epoch: float
) -> CostReportService:
    """Build an independently constructed service over its own store on a shared table."""
    store = DynamoDbCostSnapshotStore("shared-table", client_factory=lambda: table, now=lambda: now_epoch)
    return CostReportService(
        client_factory=lambda: ce_client,
        store=store,
        now=lambda: _FIXED_NOW,
        report_id_factory=lambda: report_id,
        ttl_seconds=1800,
    )


class TestCrossWorkerReuse:
    def test_reader_reuses_writer_snapshot_without_calling_cost_explorer(self):
        table = FakeDynamoDbTable()
        writer_ce = _ce_client()
        reader_ce = _ce_client()
        writer = _service_for(table, writer_ce, report_id="cost-xw", now_epoch=float(_FIXED_EPOCH))
        reader = _service_for(table, reader_ce, report_id="unused", now_epoch=float(_FIXED_EPOCH))

        token = set_request_scope(_TRUSTED_SCOPE)
        try:
            created = writer.create_report("2026-05-01", "2026-05-15")
            reused = reader.reuse_report(created.report.report_id)
        finally:
            reset_request_scope(token)

        assert reused.report_id == created.report.report_id
        # Reader must never issue a Cost Explorer query.
        reader_ce.get_cost_and_usage.assert_not_called()
        writer_ce.get_cost_and_usage.assert_called_once()

    def test_reader_with_wrong_scope_gets_generic_not_found(self):
        table = FakeDynamoDbTable()
        writer = _service_for(table, _ce_client(), report_id="cost-xw", now_epoch=float(_FIXED_EPOCH))
        reader = _service_for(table, _ce_client(), report_id="unused", now_epoch=float(_FIXED_EPOCH))

        token = set_request_scope(_TRUSTED_SCOPE)
        try:
            created = writer.create_report("2026-05-01", "2026-05-15")
        finally:
            reset_request_scope(token)

        token = set_request_scope(ReportScope(tenant="tenant-1", workspace="ws-1", actor="mallory"))
        try:
            with pytest.raises(CostReportError) as raised:
                reader.reuse_report(created.report.report_id)
        finally:
            reset_request_scope(token)
        assert raised.value.code == "COST_REPORT_NOT_FOUND"

    def test_reader_after_expiry_gets_generic_not_found(self):
        table = FakeDynamoDbTable()
        writer = _service_for(table, _ce_client(), report_id="cost-xw", now_epoch=float(_FIXED_EPOCH))
        # Reader's clock is past the snapshot's logical expiry (created + 1800s).
        reader = _service_for(table, _ce_client(), report_id="unused", now_epoch=float(_FIXED_EPOCH + 1800))

        token = set_request_scope(_TRUSTED_SCOPE)
        try:
            created = writer.create_report("2026-05-01", "2026-05-15")
            with pytest.raises(CostReportError) as raised:
                reader.reuse_report(created.report.report_id)
        finally:
            reset_request_scope(token)
        assert raised.value.code == "COST_REPORT_NOT_FOUND"

    def test_immutable_collision_on_duplicate_report_id(self):
        table = FakeDynamoDbTable()
        store = DynamoDbCostSnapshotStore("shared-table", client_factory=lambda: table, now=lambda: float(_FIXED_EPOCH))
        snapshot = _service_for(table, _ce_client(), report_id="cost-dup", now_epoch=float(_FIXED_EPOCH))

        token = set_request_scope(_TRUSTED_SCOPE)
        try:
            created = snapshot.create_report("2026-05-01", "2026-05-15")
        finally:
            reset_request_scope(token)

        # A second write to the same report ID (even cross-scope) must collide.
        colliding = SnapshotRecord(created.report.report_id, "other-scope-hash", created, _FIXED_EPOCH + 1800)
        with pytest.raises(SnapshotCollisionError):
            store.put(colliding)

    def test_second_create_with_same_report_id_fails_closed(self):
        table = FakeDynamoDbTable()
        first = _service_for(table, _ce_client(), report_id="cost-fixed", now_epoch=float(_FIXED_EPOCH))
        second_ce = _ce_client()
        second = _service_for(table, second_ce, report_id="cost-fixed", now_epoch=float(_FIXED_EPOCH))

        token = set_request_scope(_TRUSTED_SCOPE)
        try:
            first.create_report("2026-05-01", "2026-05-15")
            with pytest.raises(CostReportError) as raised:
                second.create_report("2026-05-01", "2026-05-15")
        finally:
            reset_request_scope(token)
        # Fails closed on the immutable collision; the store surfaces STORE_UNAVAILABLE.
        assert raised.value.code == "COST_REPORT_STORE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Combined-ready coverage hook (#365 + #364).
#
# When #364 (the cross-session/worker follow-up work) lands, its reader path can
# be exercised against the same FakeDynamoDbTable + two-independent-store pattern
# used above. This module deliberately does NOT import #364 yet; the hook exists
# so the combined coverage has a documented, ready seam to extend rather than a
# rewrite. Keep FakeDynamoDbTable and _service_for stable for that reuse.
#
# Real-table (deployed DynamoDB) verification is a pipeline/deployment step, not
# a unit test: the runtime IAM grants only GetItem/PutItem (no DeleteItem) and
# TTL deletion is asynchronous, so a unit run cannot create-and-clean a real
# item safely. It is documented in the PR description instead of being encoded
# as a permanently skipped placeholder test that provides no executable
# coverage.
# ---------------------------------------------------------------------------
