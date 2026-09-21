"""Safe structured diagnostic logging at the store's exception boundaries (#413).

Live diagnosis (demo / us-west-2, 2026-09-21) of an E1 valid POST that returned
``503 PROVIDER_UNAVAILABLE`` ("observation state store is temporarily
unavailable") with no table item written found a *diagnostic blind spot*: the
``DynamoDbObservationStore`` maps every non-conditional ``TransactWriteItems``
failure to ``PROVIDER_UNAVAILABLE`` and swallows the exception with no log line.
The only signal was at the AWS layer (DynamoDB ``UserErrors`` HTTP 400, while the
sole successful ``TransactWriteItems`` was an out-of-band admin control write) —
the running Lambda emitted nothing that named the failing operation or the AWS
error/cancellation codes.

These tests pin a fix that logs, at *every* store exception boundary, a bounded
structured record carrying only:

* the store operation name (a static literal we pass, never request-derived),
* the exception TYPE name (``type(exc).__name__``),
* the bounded AWS error code (``exc.response["Error"]["Code"]``), and
* the bounded transaction cancellation-reason codes
  (``exc.response["CancellationReasons"][].Code``).

Reconciling #409's traceback policy: #409 switched general call sites to
``logger.exception`` so the class+message+frames reach CloudWatch (with
``diagnose=False`` stripping local VALUES). This boundary is stricter — the
store handles idempotency tokens, operation ids, canonical intent, and request
items, any of which can appear in a botocore exception MESSAGE or in stack
locals. So this boundary MUST NOT emit the exception message, ``str(exc)``, a
traceback, or ``exc_info``; it emits sanitized metadata only. These tests prove
that with secret-bearing fake exceptions (no leak) and real botocore errors
(useful codes present).
"""

from __future__ import annotations

# Standard library
import io
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError
from loguru import logger

# Local modules
from operations.observation import (
    ObservationBeginOutcome,
    ObservationCompleteOutcome,
)
from operations.observation_store import DynamoDbObservationStore

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
TABLE = "operations-observations-test"
WORKSPACE = "workspace.default"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
FINGERPRINT = "sha256:" + "a" * 64
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
HOLDER = "request.observe-1"
INTENT = {"phase": "observe", "provider": "gamelift", "target": {"provider": "gamelift", "fleet_id": "fleet-abc"}}

# A value that stands in for request-derived data (an idempotency token, an
# operation id, a fleet id). If it ever reaches the log sink the boundary is
# leaking. It is embedded in the fake exception's MESSAGE and args.
_SECRET = "SECRET-idem-token-obs-fleet-should-never-be-logged"


def _observation() -> dict[str, Any]:
    return {
        "observation_id": OPERATION_ID,
        "phase": "observe",
        "provider": "gamelift",
        "results": {"utilization": {}, "capacity": [], "scaling_policies": []},
    }


class _ProductionSink:
    """A loguru sink configured exactly like the production stdout handler
    (``backtrace=False, diagnose=False`` per utils/logger.py)."""

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        # Serialize the record so structured ``extra`` fields are asserted on the
        # wire shape, not just the rendered message.
        self._sink_id = logger.add(
            self._buffer,
            level="DEBUG",
            format="{level} | {name}:{function}:{line} | {message} | {extra}",
            backtrace=False,
            diagnose=False,
        )

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


class _SecretBearingError(Exception):
    """A non-botocore exception whose message and args carry request-derived
    data. The boundary must log its TYPE but never its message/args."""

    def __init__(self) -> None:
        super().__init__(f"internal failure while writing {_SECRET}")


def _transaction_canceled(*reason_codes: str) -> ClientError:
    """A real botocore ClientError shaped like DynamoDB's wire response, whose
    MESSAGE carries a secret so a leak of the message is detectable."""
    return ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": f"Transaction cancelled while writing {_SECRET}",
            },
            "CancellationReasons": [{"Code": code} for code in reason_codes],
        },
        "TransactWriteItems",
    )


def _validation_error() -> ClientError:
    """A real botocore ValidationException whose message carries a secret."""
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": f"Invalid item near {_SECRET}"}},
        "TransactWriteItems",
    )


class _StatefulDynamoClient:
    """Minimal stateful DynamoDB fake (conditional writes raise a real
    conditional-only cancel)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
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


def _store(client: Any) -> DynamoDbObservationStore:
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


def _client_raising(exc: BaseException) -> _StatefulDynamoClient:
    client = _StatefulDynamoClient()

    def _raise(**_kwargs: Any) -> None:
        raise exc

    client.transact_write_items = _raise  # type: ignore[method-assign]
    return client


# --- begin_observation boundary ---------------------------------------------


def test_begin_unavailable_logs_useful_codes_from_real_botocore_error() -> None:
    """A real ValidationException at begin -> PROVIDER_UNAVAILABLE AND a log
    record naming the operation, exception type, and AWS error code."""
    client = _client_raising(_validation_error())
    with _ProductionSink() as buffer:
        begin = _begin(_store(client))
        output = buffer.getvalue()

    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE
    assert "begin_observation" in output
    assert "ClientError" in output  # exception TYPE
    assert "ValidationException" in output  # bounded AWS error code
    # Secret in the exception MESSAGE must never reach the sink.
    assert _SECRET not in output
    # No traceback frames at this boundary (sanitized metadata only).
    assert "Traceback (most recent call last)" not in output


def test_begin_unavailable_logs_cancellation_reason_codes() -> None:
    """A transient TransactionCanceledException logs its bounded cancellation
    reason codes so the operator can tell throttle from conflict."""
    client = _client_raising(_transaction_canceled("ThrottlingError"))
    with _ProductionSink() as buffer:
        begin = _begin(_store(client))
        output = buffer.getvalue()

    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE
    assert "begin_observation" in output
    assert "TransactionCanceledException" in output
    assert "ThrottlingError" in output  # bounded cancellation reason code
    assert _SECRET not in output
    assert "Traceback (most recent call last)" not in output


def test_begin_non_botocore_exception_logs_type_only_no_leak() -> None:
    """A non-botocore exception whose message carries a secret logs only its
    TYPE; the message/args never reach the sink."""
    client = _client_raising(_SecretBearingError())
    with _ProductionSink() as buffer:
        begin = _begin(_store(client))
        output = buffer.getvalue()

    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE
    assert "begin_observation" in output
    assert "_SecretBearingError" in output  # exception TYPE only
    assert _SECRET not in output
    assert "Traceback (most recent call last)" not in output


def test_begin_pure_conditional_does_not_log_unavailable() -> None:
    """A genuine idempotency race (pure ConditionalCheckFailed) is NOT a
    provider fault: it must resolve to the existing op without emitting an
    unavailable diagnostic."""
    client = _StatefulDynamoClient()
    store = _store(client)
    _begin(store)  # seed the mapping
    with _ProductionSink() as buffer:
        retry = _begin(store, operation_id="obs_" + "b" * 26)
        output = buffer.getvalue()

    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS
    # The store MUST NOT cry "unavailable" for a normal idempotency race.
    assert "dynamodb_store_exception" not in output


# --- complete_observation boundary ------------------------------------------


def _seed_created(store: DynamoDbObservationStore, client: _StatefulDynamoClient) -> None:
    begin = _begin(store)
    assert begin.outcome is ObservationBeginOutcome.CREATED


def test_complete_unavailable_logs_useful_codes() -> None:
    """A non-conditional failure at complete -> PROVIDER_UNAVAILABLE AND a log
    record naming complete_observation and the AWS error code."""
    client = _StatefulDynamoClient()
    store = _store(client)
    _seed_created(store, client)

    # Now force the finalize transaction to raise a real ValidationException.
    def _raise(**_kwargs: Any) -> None:
        raise _validation_error()

    client.transact_write_items = _raise  # type: ignore[method-assign]

    with _ProductionSink() as buffer:
        complete = store.complete_observation(
            operation_id=OPERATION_ID,
            workspace_id=WORKSPACE,
            lease_holder=HOLDER,
            commit_not_after=NOW + timedelta(minutes=30),
            ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
            observation=_observation(),
        )
        output = buffer.getvalue()

    assert complete.outcome is ObservationCompleteOutcome.PROVIDER_UNAVAILABLE
    assert "complete_observation" in output
    assert "ValidationException" in output
    assert _SECRET not in output
    assert "Traceback (most recent call last)" not in output


# --- fail_observation boundary (best-effort) --------------------------------


def test_fail_observation_swallows_but_logs_type() -> None:
    """fail_observation is best-effort (a fail record is not mandatory), but it
    must still leave a bounded breadcrumb naming the boundary and error code
    rather than vanishing silently."""
    client = _StatefulDynamoClient()
    store = _store(client)
    _seed_created(store, client)

    def _raise(**_kwargs: Any) -> None:
        raise _validation_error()

    client.transact_write_items = _raise  # type: ignore[method-assign]

    with _ProductionSink() as buffer:
        result = store.fail_observation(
            operation_id=OPERATION_ID,
            workspace_id=WORKSPACE,
            lease_holder=HOLDER,
            reason_code="provider_error",
            ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
            generation=1,
        )
        output = buffer.getvalue()

    assert result is None  # best-effort: still returns None
    assert "fail_observation" in output
    assert "ValidationException" in output
    assert _SECRET not in output
    assert "Traceback (most recent call last)" not in output


# --- _get boundary (load_status / complete replay) --------------------------


def test_get_boundary_logs_and_reraises_useful_codes() -> None:
    """A read failure (get_item) at the load_status boundary must not vanish:
    it logs a bounded record and the exception still propagates (reads are not
    silently swallowed into a false 'not found')."""

    class _ReadFailingClient(_StatefulDynamoClient):
        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            raise _validation_error()

    store = _store(_ReadFailingClient())
    with _ProductionSink() as buffer:
        with pytest.raises(ClientError):
            store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE)
        output = buffer.getvalue()

    assert "read_item" in output
    assert "ValidationException" in output
    assert _SECRET not in output
    assert "Traceback (most recent call last)" not in output
