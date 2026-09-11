"""Shared, encrypted DynamoDB snapshot store for cross-worker report reuse.

Persists validated cost report snapshots to an on-demand DynamoDB table so any
runtime worker can resolve a report ID for its configured TTL. The table itself
provides encryption at rest and TTL-based expiration (via the ``expiresAt``
attribute); this client uses only ``GetItem`` and ``PutItem`` — never a scan —
and reads with strong consistency so a snapshot written by one worker is
immediately visible to another.

Writes are immutable: a conditional ``PutItem`` refuses to overwrite an existing
report ID, and a failed condition surfaces as a typed collision. TTL deletion in
DynamoDB is asynchronous, so a defensive logical-expiry check is applied on read.

Failures fail closed: a write that does not complete raises
:class:`~agents.cost_snapshot_store.CostSnapshotStoreError` so the caller never
returns a report ID that was not durably stored, and a read that cannot complete
raises the same error rather than masquerading as a clean not-found.
"""

from __future__ import annotations

# Standard library
import time
from typing import TYPE_CHECKING, Any, Callable

# Third-party packages
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Local modules
from agents.cost_snapshot_serialization import (
    ATTR_REPORT_ID,
    MAX_ITEM_BYTES,
    estimate_item_size,
    record_from_item,
    record_to_item,
)
from agents.cost_snapshot_store import CostSnapshotStoreError, SnapshotCollisionError, SnapshotRecord
from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG
from utils.logger import logger

if TYPE_CHECKING:
    # Local modules
    from agents.cost_report import CostReportSnapshot


def _default_dynamodb_client() -> Any:
    return boto3.client("dynamodb", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)


class DynamoDbCostSnapshotStore:
    """Scoped, TTL-bounded, immutable snapshot store backed by a DynamoDB table."""

    # Shared, cross-tenant store: a trusted (authenticated, non-anonymous) scope
    # is mandatory before a snapshot may be written or resolved.
    requires_trusted_scope = True

    def __init__(
        self,
        table_name: str,
        *,
        client_factory: Callable[[], Any] = _default_dynamodb_client,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._table_name = table_name
        self._client_factory = client_factory
        self._now = now

    def put(self, record: SnapshotRecord) -> None:
        # Build and size the item inside the guarded block so a serialization
        # error becomes a typed store failure rather than an unhandled crash.
        try:
            item = record_to_item(record)
            item_size = estimate_item_size(item)
            if item_size > MAX_ITEM_BYTES:
                raise CostSnapshotStoreError(
                    f"cost report snapshot item ({item_size} bytes) exceeds the {MAX_ITEM_BYTES}-byte budget"
                )
            client = self._client_factory()
            # Immutable write: never overwrite an existing report ID.
            client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression=f"attribute_not_exists({ATTR_REPORT_ID})",
            )
        except CostSnapshotStoreError:
            # Already typed (budget check); do not re-wrap or re-log the payload.
            raise
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # A report ID already exists; snapshots are immutable. Fail closed
                # as a typed collision and never re-query Cost Explorer.
                logger.error("Cost report snapshot collision on immutable write", exc_info=True)
                raise SnapshotCollisionError("cost report snapshot already exists") from exc
            logger.error("Failed to persist cost report snapshot to shared store", exc_info=True)
            raise CostSnapshotStoreError("cost report snapshot could not be persisted") from exc
        except BotoCoreError as exc:
            logger.error("Failed to persist cost report snapshot to shared store", exc_info=True)
            raise CostSnapshotStoreError("cost report snapshot could not be persisted") from exc
        except Exception as exc:
            # Serialization or any other unexpected error: fail closed as a typed
            # store failure so the caller never returns an unreusable report ID.
            logger.error("Unexpected error persisting cost report snapshot", exc_info=True)
            raise CostSnapshotStoreError("cost report snapshot could not be persisted") from exc

    def get(self, report_id: str, scope_hash: str) -> "CostReportSnapshot | None":
        try:
            client = self._client_factory()
            response = client.get_item(
                TableName=self._table_name,
                Key={ATTR_REPORT_ID: {"S": report_id}},
                ConsistentRead=True,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("Failed to read cost report snapshot from shared store", exc_info=True)
            raise CostSnapshotStoreError("cost report snapshot could not be read") from exc

        item = response.get("Item")
        if not item:
            return None

        record = record_from_item(item)
        if record is None or record.scope_hash != scope_hash:
            return None

        # Defensive TTL check: DynamoDB deletes expired items only eventually, so
        # never reuse a snapshot past its logical expiry even if it is still
        # present. Expiry is exact: a record whose expires_at equals now is expired.
        if record.expires_at <= int(self._now()):
            return None

        return record.snapshot
