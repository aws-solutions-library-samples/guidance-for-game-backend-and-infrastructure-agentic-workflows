"""Unit tests for the shared cost report snapshot store (#365).

Covers serialization round-trips, strict fail-closed decode, TTL metadata and
exact logical-expiry boundaries, scope enforcement (including trusted-scope
rejection), the DynamoDB request shape, immutable conditional writes, item-size
budgeting, fail-closed put/get behavior, and default store selection. All
amounts and identifiers are synthetic.
"""

# Standard library
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from agents.cost_report import CostReportError, CostReportService
from agents.cost_report_scope import (
    ANONYMOUS_ACTOR,
    ReportScope,
    ScopeAuthorizationError,
    current_scope_hash,
    is_trusted_scope,
    reset_request_scope,
    resolve_scope_actor,
    scope_hash,
    set_request_scope,
)
from agents.cost_snapshot_dynamodb import DynamoDbCostSnapshotStore
from agents.cost_snapshot_memory import InMemoryCostSnapshotStore
from agents.cost_snapshot_serialization import (
    ATTR_EXPIRES_AT,
    ATTR_REPORT_ID,
    ATTR_SCHEMA_VERSION,
    ATTR_SCOPE_HASH,
    ATTR_SNAPSHOT,
    MAX_ITEM_BYTES,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotSerializationError,
    estimate_item_size,
    record_from_item,
    record_to_item,
    snapshot_from_json,
    snapshot_to_json,
)
from agents.cost_snapshot_store import (
    CostSnapshotStore,
    CostSnapshotStoreError,
    SnapshotCollisionError,
    SnapshotRecord,
    build_default_snapshot_store,
)

pytestmark = pytest.mark.unit

_FIXED_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
_FIXED_EPOCH = int(_FIXED_NOW.timestamp())


def _page(groups: list[dict]) -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01", "End": "2026-05-16"},
                "Estimated": False,
                "Groups": groups,
            }
        ]
    }


def _groups() -> list[dict]:
    return [
        {"Keys": ["Amazon EKS"], "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}}},
        {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "20.00", "Unit": "USD"}}},
    ]


def _service(
    store: CostSnapshotStore, *, report_id: str = "cost-shared-1", ttl_seconds: int = 1800
) -> CostReportService:
    client = MagicMock()
    client.get_cost_and_usage.return_value = _page(_groups())
    return CostReportService(
        client_factory=lambda: client,
        store=store,
        now=lambda: _FIXED_NOW,
        report_id_factory=lambda: report_id,
        ttl_seconds=ttl_seconds,
    )


def _make_snapshot(report_id: str = "cost-shared-1"):
    """Create a valid snapshot whose report ID matches ``report_id``."""
    return _service(InMemoryCostSnapshotStore(), report_id=report_id).create_report("2026-05-01", "2026-05-15")


class _CapturingStore:
    """A minimal in-process store that records the last put record."""

    requires_trusted_scope = False

    def __init__(self) -> None:
        self.records: dict[str, SnapshotRecord] = {}
        self.put_error: Exception | None = None
        self.get_error: Exception | None = None

    def put(self, record: SnapshotRecord) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.records[record.report_id] = record

    def get(self, report_id: str, scope_hash_value: str):
        if self.get_error is not None:
            raise self.get_error
        record = self.records.get(report_id)
        if record is None or record.scope_hash != scope_hash_value:
            return None
        return record.snapshot


class TestSerialization:
    def test_snapshot_json_round_trip_preserves_report_and_aggregate_amounts(self):
        snapshot = _make_snapshot()

        restored = snapshot_from_json(snapshot_to_json(snapshot))

        assert restored.report.model_dump(by_alias=True, mode="json") == snapshot.report.model_dump(
            by_alias=True, mode="json"
        )
        # Canonical aggregates are preserved exactly; raw source amounts are not
        # persisted (not needed for reuse) and restore as empty tuples.
        assert [(r.service, r.amount) for r in restored.raw_services] == [
            (r.service, r.amount) for r in snapshot.raw_services
        ]
        assert all(r.source_amounts == () for r in restored.raw_services)
        assert restored.raw_services[0].amount == Decimal("80.00")

    def test_persisted_payload_omits_raw_source_amounts(self):
        payload = json.loads(snapshot_to_json(_make_snapshot()))
        assert set(payload) == {"report", "services"}
        for entry in payload["services"]:
            assert set(entry) == {"service", "amount"}

    def test_item_carries_ttl_scope_and_schema_metadata(self):
        record = SnapshotRecord(
            report_id="cost-item-1",
            scope_hash="hash-1",
            snapshot=_make_snapshot("cost-item-1"),
            expires_at=1_700_000_000,
        )

        item = record_to_item(record)

        assert item[ATTR_REPORT_ID] == {"S": "cost-item-1"}
        assert item[ATTR_SCOPE_HASH] == {"S": "hash-1"}
        assert item[ATTR_EXPIRES_AT] == {"N": "1700000000"}
        assert item[ATTR_SCHEMA_VERSION] == {"S": SNAPSHOT_SCHEMA_VERSION}
        assert ATTR_SNAPSHOT in item

    def test_round_trip_through_item_restores_record(self):
        record = SnapshotRecord("cost-item-2", "hash-2", _make_snapshot("cost-item-2"), 1_700_000_000)

        restored = record_from_item(record_to_item(record))

        assert restored is not None
        assert restored.report_id == "cost-item-2"
        assert restored.scope_hash == "hash-2"
        assert restored.expires_at == 1_700_000_000

    def test_malformed_or_version_mismatched_items_are_rejected(self):
        assert record_from_item({}) is None
        assert record_from_item({ATTR_REPORT_ID: {"S": "x"}}) is None

        item = record_to_item(SnapshotRecord("cost-x", "h", _make_snapshot("cost-x"), 10))
        item[ATTR_SCHEMA_VERSION] = {"S": "9.9"}
        assert record_from_item(item) is None

    def test_outer_inner_report_id_mismatch_is_rejected(self):
        item = record_to_item(SnapshotRecord("cost-inner", "h", _make_snapshot("cost-inner"), 10))
        # Corrupt the outer key so it disagrees with the embedded report ID.
        item[ATTR_REPORT_ID] = {"S": "cost-outer-different"}
        assert record_from_item(item) is None

    def test_decode_fails_closed_on_tampered_total(self):
        snapshot = _make_snapshot()
        payload = json.loads(snapshot_to_json(snapshot))
        # Tamper: inflate the persisted total so it no longer reconciles with the
        # canonical reconstruction from the service aggregates.
        payload["report"]["total"] = "999999.00"
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(json.dumps(payload))

    def test_decode_rejects_duplicate_service(self):
        snapshot = _make_snapshot()
        payload = json.loads(snapshot_to_json(snapshot))
        payload["services"].append(dict(payload["services"][0]))
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(json.dumps(payload))

    def test_decode_rejects_empty_service_name(self):
        snapshot = _make_snapshot()
        payload = json.loads(snapshot_to_json(snapshot))
        payload["services"][0]["service"] = ""
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(json.dumps(payload))

    @pytest.mark.parametrize("bad_amount", ["not-a-number", "NaN", "Infinity", "1" * 80, "1e20"])
    def test_decode_rejects_bad_aggregate_amounts(self, bad_amount):
        snapshot = _make_snapshot()
        payload = json.loads(snapshot_to_json(snapshot))
        payload["services"][0]["amount"] = bad_amount
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(json.dumps(payload))

    def test_decode_rejects_bad_metadata(self):
        snapshot = _make_snapshot()
        payload = json.loads(snapshot_to_json(snapshot))
        payload["report"]["source"] = "Somewhere Else"
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(json.dumps(payload))

    def test_period_overflow_fails_closed_at_snapshot_and_record_boundaries(self):
        snapshot = _make_snapshot("cost-period-overflow")
        item = record_to_item(SnapshotRecord("cost-period-overflow", "h", snapshot, 1_700_000_000))
        payload = json.loads(item[ATTR_SNAPSHOT]["S"])
        payload["report"]["period"] = {
            "start": "9999-12-30",
            "endInclusive": "9999-12-31",
            "endExclusive": "9999-12-31",
        }
        corrupted = json.dumps(payload)

        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(corrupted)

        item[ATTR_SNAPSHOT] = {"S": corrupted}
        assert record_from_item(item) is None

    def test_decode_rejects_non_json(self):
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json("{not json")

    def test_decode_fails_closed_on_near_canceling_aggregate_arithmetic(self):
        """An adversarial-but-finite payload whose aggregates nearly cancel drives
        the canonical rebuild's percentage computation past the decimal context
        precision (raising decimal.InvalidOperation inside _build_report). That
        arithmetic failure previously escaped record_from_item as an unhandled
        crash; it must now surface as a fail-closed serialization error.

        Each amount is individually finite and within the accepted magnitude and
        length bounds, so it passes _parse_service_amount — the failure only
        manifests during the canonical reconstruction from the aggregates.
        """
        snapshot = _make_snapshot()
        payload = json.loads(snapshot_to_json(snapshot))
        # Two aggregates that nearly cancel to a tiny non-zero total, which blows
        # up amount * 100 / total_raw when the percentage is quantized.
        payload["services"] = [
            {"service": "Amazon EKS", "amount": "999999999999999.9999999999999"},
            {"service": "Amazon S3", "amount": "-999999999999999.9999999999998"},
        ]
        with pytest.raises(SnapshotSerializationError):
            snapshot_from_json(json.dumps(payload))

    def test_decode_arithmetic_failure_resolves_to_generic_not_found(self):
        """record_from_item must translate the arithmetic failure into a generic
        not-found (None) rather than propagating the raw decimal exception."""
        snapshot = _make_snapshot("cost-near-cancel")
        item = record_to_item(SnapshotRecord("cost-near-cancel", "h", snapshot, 1_700_000_000))
        payload = json.loads(item[ATTR_SNAPSHOT]["S"])
        payload["services"] = [
            {"service": "Amazon EKS", "amount": "999999999999999.9999999999999"},
            {"service": "Amazon S3", "amount": "-999999999999999.9999999999998"},
        ]
        item[ATTR_SNAPSHOT] = {"S": json.dumps(payload)}
        assert record_from_item(item) is None


class TestItemBudget:
    def test_estimate_counts_names_and_values(self):
        item = {"reportId": {"S": "abc"}, "expiresAt": {"N": "123"}}
        # len("reportId")+len("abc") + len("expiresAt")+len("123")
        assert estimate_item_size(item) == (8 + 3) + (9 + 3)

    def test_put_rejects_item_over_budget(self):
        snapshot = _make_snapshot("cost-big")
        client = MagicMock()
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)
        # Force the estimator over the budget without building a giant payload.
        # A scope hash padded past the ceiling is enough to trip the check.
        record = SnapshotRecord("cost-big", "x" * (MAX_ITEM_BYTES + 1), snapshot, 1_700_000_000)

        with pytest.raises(CostSnapshotStoreError):
            store.put(record)
        client.put_item.assert_not_called()

    def test_put_allows_item_just_under_budget(self):
        snapshot = _make_snapshot("cost-ok")
        client = MagicMock()
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)
        record = SnapshotRecord("cost-ok", "hash", snapshot, 1_700_000_000)
        assert estimate_item_size(record_to_item(record)) < MAX_ITEM_BYTES

        store.put(record)
        client.put_item.assert_called_once()


class TestScopeHelpers:
    def test_is_trusted_scope(self):
        assert is_trusted_scope(ReportScope(tenant="t", workspace="w", actor="alice"))
        assert not is_trusted_scope(ReportScope())  # UNSCOPED
        assert not is_trusted_scope(ReportScope(tenant="t", workspace="w", actor=""))
        assert not is_trusted_scope(ReportScope(tenant="t", workspace="w", actor=ANONYMOUS_ACTOR))

    def test_resolve_scope_actor_shared_success(self):
        assert resolve_scope_actor(trusted_actor="alice", body_user_id="alice", shared_mode=True) == "alice"
        assert resolve_scope_actor(trusted_actor="alice", body_user_id=None, shared_mode=True) == "alice"
        assert resolve_scope_actor(trusted_actor="alice", body_user_id="anonymous", shared_mode=True) == "alice"

    def test_resolve_scope_actor_shared_requires_trusted(self):
        for bad in (None, "", "anonymous"):
            with pytest.raises(ScopeAuthorizationError):
                resolve_scope_actor(trusted_actor=bad, body_user_id="alice", shared_mode=True)

    def test_resolve_scope_actor_shared_rejects_mismatch(self):
        with pytest.raises(ScopeAuthorizationError):
            resolve_scope_actor(trusted_actor="alice", body_user_id="mallory", shared_mode=True)

    def test_resolve_scope_actor_non_shared_falls_back_to_body(self):
        assert resolve_scope_actor(trusted_actor=None, body_user_id="alice", shared_mode=False) == "alice"
        assert resolve_scope_actor(trusted_actor="bob", body_user_id="alice", shared_mode=False) == "bob"


class TestScopeEnforcement:
    def test_reuse_across_instances_shares_one_store(self):
        store = _CapturingStore()
        creator = _service(store, report_id="cost-cross-worker")
        reader = _service(store, report_id="should-not-be-used")

        created = creator.create_report("2026-05-01", "2026-05-15")
        reused = reader.reuse_report(created.report.report_id)

        assert reused.report_id == created.report.report_id
        assert store.records[created.report.report_id].scope_hash == current_scope_hash()

    def test_scope_mismatch_returns_generic_not_found(self):
        store = InMemoryCostSnapshotStore(now=lambda: float(_FIXED_EPOCH))
        service = _service(store, report_id="cost-scoped")

        token = set_request_scope(ReportScope(tenant="t", workspace="w", actor="alice"))
        try:
            created = service.create_report("2026-05-01", "2026-05-15")
        finally:
            reset_request_scope(token)

        token = set_request_scope(ReportScope(tenant="t", workspace="w", actor="mallory"))
        try:
            with pytest.raises(CostReportError) as raised:
                service.reuse_report(created.report.report_id)
        finally:
            reset_request_scope(token)

        assert raised.value.code == "COST_REPORT_NOT_FOUND"

    def test_same_scope_resolves(self):
        store = InMemoryCostSnapshotStore(now=lambda: float(_FIXED_EPOCH))
        service = _service(store, report_id="cost-scope-match")
        scope = ReportScope(tenant="t", workspace="w", actor="alice")

        token = set_request_scope(scope)
        try:
            created = service.create_report("2026-05-01", "2026-05-15")
            reused = service.reuse_report(created.report.report_id)
        finally:
            reset_request_scope(token)

        assert reused.report_id == created.report.report_id

    def test_scope_hash_ignores_raw_identifiers(self):
        digest = scope_hash(ReportScope(tenant="tenant-1", workspace="ws-1", actor="alice"))
        assert len(digest) == 64
        assert "tenant-1" not in digest and "alice" not in digest


class TestTrustedScopeRequired:
    """A store that requires a trusted scope must reject untrusted create/reuse."""

    class _RequiringStore(_CapturingStore):
        requires_trusted_scope = True

    def test_create_rejects_unscoped_when_store_requires_trust(self):
        service = _service(self._RequiringStore(), report_id="cost-req")
        with pytest.raises(CostReportError) as raised:
            service.create_report("2026-05-01", "2026-05-15")  # UNSCOPED context
        assert raised.value.code == "COST_REPORT_SCOPE_REQUIRED"

    def test_reuse_rejects_anonymous_when_store_requires_trust(self):
        service = _service(self._RequiringStore(), report_id="cost-req")
        token = set_request_scope(ReportScope(tenant="t", workspace="w", actor=ANONYMOUS_ACTOR))
        try:
            with pytest.raises(CostReportError) as raised:
                service.reuse_report("cost-req")
        finally:
            reset_request_scope(token)
        assert raised.value.code == "COST_REPORT_SCOPE_REQUIRED"

    def test_trusted_scope_allows_create_and_reuse(self):
        service = _service(self._RequiringStore(), report_id="cost-req")
        token = set_request_scope(ReportScope(tenant="t", workspace="w", actor="alice"))
        try:
            created = service.create_report("2026-05-01", "2026-05-15")
            reused = service.reuse_report(created.report.report_id)
        finally:
            reset_request_scope(token)
        assert reused.report_id == "cost-req"


class TestCreateStoresRecord:
    def test_create_report_persists_ttl_from_queried_at(self):
        store = _CapturingStore()
        _service(store, report_id="cost-ttl", ttl_seconds=1800).create_report("2026-05-01", "2026-05-15")

        record = store.records["cost-ttl"]
        assert record.expires_at == _FIXED_EPOCH + 1800

    def test_create_report_fails_closed_when_store_write_fails(self):
        store = _CapturingStore()
        store.put_error = CostSnapshotStoreError("boom")

        with pytest.raises(CostReportError) as raised:
            _service(store, report_id="cost-fail").create_report("2026-05-01", "2026-05-15")

        assert raised.value.code == "COST_REPORT_STORE_UNAVAILABLE"
        assert raised.value.retryable is True

    def test_create_report_fails_closed_on_collision(self):
        store = _CapturingStore()
        store.put_error = SnapshotCollisionError("exists")

        with pytest.raises(CostReportError) as raised:
            _service(store, report_id="cost-collide").create_report("2026-05-01", "2026-05-15")

        assert raised.value.code == "COST_REPORT_STORE_UNAVAILABLE"

    def test_reuse_fails_closed_when_store_read_fails(self):
        store = _CapturingStore()
        service = _service(store, report_id="cost-read-fail")
        service.create_report("2026-05-01", "2026-05-15")
        store.get_error = CostSnapshotStoreError("boom")

        with pytest.raises(CostReportError) as raised:
            service.reuse_report("cost-read-fail")

        assert raised.value.code == "COST_REPORT_NOT_FOUND"


class TestInMemoryExpiry:
    def test_exact_expiry_boundary(self):
        clock = {"t": 100.0}
        store = InMemoryCostSnapshotStore(now=lambda: clock["t"])
        snapshot = _make_snapshot("cost-mem")
        store.put(SnapshotRecord("cost-mem", "h", snapshot, expires_at=200))

        clock["t"] = 199.0
        assert store.get("cost-mem", "h") is not None
        clock["t"] = 200.0  # expires_at == now -> expired
        assert store.get("cost-mem", "h") is None

    def test_bounded_size_evicts_oldest(self):
        store = InMemoryCostSnapshotStore(maxsize=1, now=lambda: 0.0)
        store.put(SnapshotRecord("a", "h", _make_snapshot("a"), expires_at=10_000))
        store.put(SnapshotRecord("b", "h", _make_snapshot("b"), expires_at=10_000))
        assert store.get("a", "h") is None
        assert store.get("b", "h") is not None


class TestDynamoDbStore:
    def test_put_uses_conditional_write_and_table(self):
        snapshot = _make_snapshot("cost-ddb")
        client = MagicMock()
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        store.put(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 1_700_000_000))

        client.put_item.assert_called_once()
        kwargs = client.put_item.call_args.kwargs
        assert kwargs["TableName"] == "snap-table"
        assert kwargs["Item"][ATTR_REPORT_ID] == {"S": "cost-ddb"}
        assert kwargs["Item"][ATTR_SCOPE_HASH] == {"S": "hash-ddb"}
        assert kwargs["ConditionExpression"] == f"attribute_not_exists({ATTR_REPORT_ID})"

    def test_put_collision_raises_typed_error(self):
        snapshot = _make_snapshot("cost-ddb")
        client = MagicMock()
        client.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        with pytest.raises(SnapshotCollisionError):
            store.put(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 1_700_000_000))

    def test_get_uses_strongly_consistent_key_lookup(self):
        snapshot = _make_snapshot("cost-ddb")
        client = MagicMock()
        client.get_item.return_value = {
            "Item": record_to_item(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 1_700_000_000))
        }
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        result = store.get("cost-ddb", "hash-ddb")

        client.get_item.assert_called_once_with(
            TableName="snap-table",
            Key={ATTR_REPORT_ID: {"S": "cost-ddb"}},
            ConsistentRead=True,
        )
        assert result is not None
        assert result.report.report_id == snapshot.report.report_id

    def test_get_returns_none_on_scope_mismatch(self):
        snapshot = _make_snapshot("cost-ddb")
        client = MagicMock()
        client.get_item.return_value = {
            "Item": record_to_item(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 1_700_000_000))
        }
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        assert store.get("cost-ddb", "different-hash") is None

    def test_get_returns_none_when_absent(self):
        client = MagicMock()
        client.get_item.return_value = {}
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        assert store.get("missing", "hash") is None

    def test_get_exact_expiry_boundary(self):
        snapshot = _make_snapshot("cost-ddb")
        client = MagicMock()
        client.get_item.return_value = {"Item": record_to_item(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 100))}
        # expires_at == now -> expired
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)
        assert store.get("cost-ddb", "hash-ddb") is None

        client2 = MagicMock()
        client2.get_item.return_value = {"Item": record_to_item(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 101))}
        store2 = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client2, now=lambda: 100.0)
        assert store2.get("cost-ddb", "hash-ddb") is not None

    def test_put_failure_raises_store_error(self):
        snapshot = _make_snapshot("cost-ddb")
        client = MagicMock()
        client.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "PutItem",
        )
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        with pytest.raises(CostSnapshotStoreError):
            store.put(SnapshotRecord("cost-ddb", "hash-ddb", snapshot, 1_700_000_000))

    def test_get_failure_raises_store_error(self):
        client = MagicMock()
        client.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "unavailable"}},
            "GetItem",
        )
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)

        with pytest.raises(CostSnapshotStoreError):
            store.get("cost-ddb", "hash-ddb")

    def test_serialization_failure_translated_to_store_error(self):
        client = MagicMock()
        store = DynamoDbCostSnapshotStore("snap-table", client_factory=lambda: client, now=lambda: 100.0)
        broken = MagicMock()  # not a real snapshot; record_to_item will fail

        with pytest.raises(CostSnapshotStoreError):
            store.put(SnapshotRecord("cost-broken", "hash", broken, 1_700_000_000))
        client.put_item.assert_not_called()


class TestDefaultStoreSelection:
    def test_local_mode_without_table_uses_in_memory_store(self, monkeypatch):
        # Local modules
        import config.settings as settings

        monkeypatch.setattr(settings, "COST_SNAPSHOT_TABLE_NAME", None, raising=False)
        monkeypatch.setattr(settings, "COST_SNAPSHOT_STORE_REQUIRED", False, raising=False)

        assert isinstance(build_default_snapshot_store(), InMemoryCostSnapshotStore)

    def test_required_without_table_fails_closed(self, monkeypatch):
        # Local modules
        import config.settings as settings

        monkeypatch.setattr(settings, "COST_SNAPSHOT_TABLE_NAME", None, raising=False)
        monkeypatch.setattr(settings, "COST_SNAPSHOT_STORE_REQUIRED", True, raising=False)

        with pytest.raises(CostSnapshotStoreError):
            build_default_snapshot_store()

    def test_configured_table_selects_dynamodb_store(self, monkeypatch):
        # Local modules
        import config.settings as settings

        monkeypatch.setattr(settings, "COST_SNAPSHOT_TABLE_NAME", "snap-table", raising=False)

        assert isinstance(build_default_snapshot_store(), DynamoDbCostSnapshotStore)
