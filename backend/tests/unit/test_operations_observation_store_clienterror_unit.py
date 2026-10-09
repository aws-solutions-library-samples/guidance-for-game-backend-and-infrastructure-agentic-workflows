"""Real-botocore ClientError classification for the observation store (#413).

These tests close a production blind spot: the store must classify a genuine
``botocore.exceptions.ClientError`` carrying ``Error.Code`` =
``TransactionCanceledException`` and ``response["CancellationReasons"]`` — the
actual wire shape — not a hand-rolled exception with a non-existent
``cancellation_reasons`` attribute. A pure ``ConditionalCheckFailed`` is an
idempotency/state race; every transient/validation/unknown reason must fail
closed as ``PROVIDER_UNAVAILABLE``. A bare ``ConditionalCheckFailedException``
(non-transactional write) stays conditional.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.observation import ObservationBeginOutcome
from operations.observation_store import DynamoDbObservationStore

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
TABLE = "operations-observations-test"
WORKSPACE = "workspace.default"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
FINGERPRINT = "sha256:" + "a" * 64
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
HOLDER = "request.observe-1"
INTENT = {"phase": "observe", "provider": "gamelift", "target": {"provider": "gamelift", "fleet_id": "fleet-abc"}}


def _transaction_canceled(*reason_codes: str) -> ClientError:
    """Build a real ClientError shaped exactly like DynamoDB's wire response.

    ``CancellationReasons`` is a positional list aligned to the transaction's
    legs; a non-failing leg reports ``{"Code": "None"}``. Reasons live inside
    ``response`` — botocore never exposes a ``cancellation_reasons`` attribute.
    """
    response: dict[str, Any] = {
        "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
        "CancellationReasons": [{"Code": code} for code in reason_codes],
        "ResponseMetadata": {"HTTPStatusCode": 400},
    }
    return ClientError(response, "TransactWriteItems")


def _transaction_canceled_no_reasons() -> ClientError:
    """A TransactionCanceledException with no structured reasons list."""
    return ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"}},
        "TransactWriteItems",
    )


class StatefulDynamoClient:
    """A minimal stateful DynamoDB fake that raises real ClientErrors."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transactions.append(TransactItems)
        # First pass: evaluate conditions, raising a real conditional-only cancel.
        for entry in TransactItems:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                cond = put.get("ConditionExpression", "")
                if "attribute_not_exists(PK)" in cond and key in self.items:
                    raise _transaction_canceled("ConditionalCheckFailed")
                if "attribute_not_exists(SK)" in cond and key in self.items:
                    raise _transaction_canceled("ConditionalCheckFailed")
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}


def _store(client: StatefulDynamoClient) -> DynamoDbObservationStore:
    return DynamoDbObservationStore(client=client, table_name=TABLE, clock=lambda: NOW)


def _begin(store: DynamoDbObservationStore, *, operation_id: str = OPERATION_ID) -> Any:
    return store.begin_observation(
        operation_id=operation_id,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(minutes=30),
        lease_not_after=NOW + timedelta(seconds=15),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        intent=INTENT,
    )


def _begin_raising(exc: BaseException) -> Any:
    client = StatefulDynamoClient()

    def _raise(**_kwargs: Any) -> None:
        raise exc

    client.transact_write_items = _raise  # type: ignore[method-assign]
    return _begin(_store(client))


# --- pure ConditionalCheckFailed => idempotency/state race, not unavailable ---


def test_real_pure_conditional_reason_resolves_idempotency_not_unavailable() -> None:
    # Seed the mapping so the store's collision handler finds an in-progress op,
    # then the second begin hits a REAL TransactionCanceledException whose only
    # reason is ConditionalCheckFailed. It must resolve to the existing op, not
    # fail closed as PROVIDER_UNAVAILABLE.
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    retry = _begin(store, operation_id="obs_" + "b" * 26)
    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS


# --- transient / validation / unknown reasons => fail closed -----------------


@pytest.mark.parametrize(
    "reason_codes",
    [
        ("TransactionConflict",),
        ("TransactionConflict", "None"),
        ("ThrottlingError",),
        ("ProvisionedThroughputExceeded",),
        ("ValidationError",),
        ("SomethingElseEntirely",),
        ("ConditionalCheckFailed", "TransactionConflict"),  # mixed => transient wins
        ("ConditionalCheckFailed", "ThrottlingError"),
        (),  # empty reasons list
    ],
)
def test_real_non_conditional_reasons_fail_closed(reason_codes: tuple[str, ...]) -> None:
    begin = _begin_raising(_transaction_canceled(*reason_codes))
    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE


def test_real_transaction_canceled_without_reasons_fails_closed() -> None:
    # No CancellationReasons key at all: cannot be confirmed conditional, so it
    # must fail closed rather than be misread as an idempotency/state conflict.
    begin = _begin_raising(_transaction_canceled_no_reasons())
    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE


# --- bare ConditionalCheckFailedException (non-transactional) preserved -------


def test_real_bare_conditional_check_failed_is_conditional() -> None:
    # A non-transactional conditional write reports ConditionalCheckFailedException
    # in Error.Code with no CancellationReasons. This must remain classified as a
    # genuine conditional failure (idempotency/state resolution), unchanged.
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)  # seed the mapping

    bare = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
        "PutItem",
    )

    def _raise(**_kwargs: Any) -> None:
        raise bare

    client.transact_write_items = _raise  # type: ignore[method-assign]
    retry = _begin(store, operation_id="obs_" + "b" * 26)
    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS
