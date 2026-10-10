"""Unit tests for the two-phase conditional/idempotent DynamoDB store (#413)."""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.contracts.canonical import canonical_sha256
from operations.observation import (
    ObservationBeginOutcome,
    ObservationCompleteOutcome,
    ObservationStatusView,
)
from operations.observation_store import (
    DynamoDbObservationStore,
    ObservationStoreError,
)

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
TABLE = "operations-observations-test"
WORKSPACE = "workspace.default"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
FINGERPRINT = "sha256:" + "a" * 64
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
HOLDER = "request.observe-1"
INTENT = {"phase": "observe", "provider": "gamelift", "target": {"provider": "gamelift", "fleet_id": "fleet-abc"}}


def _observation() -> dict[str, Any]:
    return {
        "observation_id": OPERATION_ID,
        "phase": "observe",
        "provider": "gamelift",
        "results": {"utilization": {}, "capacity": [], "scaling_policies": []},
    }


def _transaction_canceled(*reason_codes: str) -> ClientError:
    """Build a real botocore ClientError shaped like DynamoDB's wire response.

    Reasons live inside ``response["CancellationReasons"]`` (a positional list
    aligned to the transaction legs). A real ClientError never exposes a
    ``cancellation_reasons`` attribute, so tests must not fabricate one.
    """
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": code} for code in reason_codes],
        },
        "TransactWriteItems",
    )


def TransactionCanceled() -> ClientError:
    """A pure ConditionalCheckFailed transaction cancellation (real shape)."""
    return _transaction_canceled("ConditionalCheckFailed")


class StatefulDynamoClient:
    """A minimal stateful DynamoDB fake honoring conditional puts and updates."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.request_tokens: list[str | None] = []
        self.gets: list[tuple[str, str]] = []
        self.raise_non_conditional = False

    def transact_write_items(
        self, *, TransactItems: list[dict[str, Any]], ClientRequestToken: str | None = None
    ) -> dict[str, Any]:
        self.transactions.append(TransactItems)
        self.request_tokens.append(ClientRequestToken)
        if self.raise_non_conditional:
            raise RuntimeError("throttled")
        # First pass: evaluate every condition against current state.
        for entry in TransactItems:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                cond = put.get("ConditionExpression", "")
                if "attribute_not_exists(PK)" in cond and key in self.items:
                    raise TransactionCanceled()
                if "attribute_not_exists(SK)" in cond and key in self.items:
                    raise TransactionCanceled()
            elif "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                if not self._update_condition_holds(upd, key):
                    raise TransactionCanceled()
        # Second pass: apply.
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            elif "Update" in entry:
                self._apply_update(entry["Update"])
        return {}

    def _update_condition_holds(self, upd: dict[str, Any], key: tuple[str, str]) -> bool:
        current = self.items.get(key)
        if current is None:
            return False
        values = upd.get("ExpressionAttributeValues", {})
        if current.get("state", {}).get("S") != values.get(":observing", {}).get("S"):
            return False
        if int(current.get("sequence", {}).get("N", "-1")) != int(values.get(":zero", {}).get("N", "0")):
            return False
        # Reclaim path: fenced on the observed generation (:cur_gen) and an
        # EXPIRED lease (lease_not_after <= :now). No holder match required.
        if ":cur_gen" in values:
            if int(current.get("generation", {}).get("N", "-1")) != int(values[":cur_gen"]["N"]):
                return False
            if int(current.get("lease_not_after", {}).get("N", "0")) > int(values[":now"]["N"]):
                return False
            return True
        # complete/fail path: fenced on the held generation, holder, and an
        # UNEXPIRED lease (lease_not_after > :now) when :now is present.
        if int(current.get("generation", {}).get("N", "-1")) != int(values.get(":gen", {}).get("N", "0")):
            return False
        if current.get("lease_holder", {}).get("S") != values.get(":holder", {}).get("S"):
            return False
        if ":now" in values:
            if int(current.get("lease_not_after", {}).get("N", "0")) <= int(values[":now"]["N"]):
                return False
        return True

    def _apply_update(self, upd: dict[str, Any]) -> None:
        key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
        current = dict(self.items[key])
        values = upd.get("ExpressionAttributeValues", {})
        if ":succeeded" in values:
            current["state"] = values[":succeeded"]
            current["sequence"] = values[":one"]
        elif ":failed" in values:
            current["state"] = values[":failed"]
            current["sequence"] = values[":one"]
            current["reason_code"] = values[":reason"]
        elif ":cur_gen" in values:
            # Reclaim: advance generation, take the lease.
            current["generation"] = values[":new_gen"]
            current["lease_holder"] = values[":holder"]
            current["lease_not_after"] = values[":new_lease"]
        self.items[key] = current

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        self.gets.append((pk, sk))
        item = self.items.get((pk, sk))
        return {"Item": item} if item is not None else {}


def _store(client: StatefulDynamoClient, clock: datetime = NOW) -> DynamoDbObservationStore:
    return DynamoDbObservationStore(client=client, table_name=TABLE, clock=lambda: clock)


def _begin(
    store: DynamoDbObservationStore, *, deadline: datetime | None = None, operation_id: str = OPERATION_ID
) -> Any:
    return store.begin_observation(
        operation_id=operation_id,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=HOLDER,
        commit_not_after=deadline or (NOW + timedelta(seconds=15)),
        lease_not_after=NOW + timedelta(seconds=15),
        intent=INTENT,
    )


def _complete(store: DynamoDbObservationStore, observation: dict[str, Any] | None = None) -> Any:
    return store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(seconds=15),
        observation=observation or _observation(),
    )


# --- begin ----------------------------------------------------------------


def test_begin_writes_four_conditional_items() -> None:
    client = StatefulDynamoClient()
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.CREATED
    items = client.transactions[0]
    assert len(items) == 4
    assert all("ConditionExpression" in entry["Put"] for entry in items)
    ledger = next(e for e in items if e["Put"]["Item"]["SK"]["S"] == "LEDGER#0")
    assert ledger["Put"]["ConditionExpression"] == "attribute_not_exists(SK)"


def test_begin_snapshot_is_observing_with_lease() -> None:
    client = StatefulDynamoClient()
    _begin(_store(client))
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "observing"
    assert snapshot["generation"]["N"] == "1"
    assert snapshot["lease_holder"]["S"] == HOLDER
    assert snapshot["workspace_id"]["S"] == WORKSPACE
    # ADR 0005: the snapshot is retained for the audit/replay window and carries
    # no DynamoDB ``ttl`` attribute.
    assert "ttl" not in snapshot


def test_begin_expired_deadline_fails_closed_without_writing() -> None:
    client = StatefulDynamoClient()
    begin = _begin(_store(client), deadline=NOW - timedelta(seconds=1))
    assert begin.outcome is ObservationBeginOutcome.DEADLINE_EXPIRED
    assert client.transactions == []


def test_begin_refuses_to_start_a_sub_call_that_would_not_fit_before_the_deadline() -> None:
    # The deadline is still in the future, but less than one full store sub-call
    # (the default 4.5 s call limit) fits before it. begin must fail closed with
    # DEADLINE_EXPIRED and write nothing, so a transaction never starts so late
    # that it could return after the Lambda timeout. A guard that only checked
    # whether the deadline was already past would wrongly start the write here.
    client = StatefulDynamoClient()
    begin = _begin(_store(client), deadline=NOW + timedelta(seconds=2.0))
    assert begin.outcome is ObservationBeginOutcome.DEADLINE_EXPIRED
    assert client.transactions == []


def test_complete_refuses_to_start_a_sub_call_that_would_not_fit_before_the_deadline() -> None:
    # Same guard on the finalize transaction: a commit is not started unless one
    # full call limit still fits before the deadline.
    client = StatefulDynamoClient()
    _begin(_store(client))
    complete = _store(client).complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(seconds=2.0),
        observation=_observation(),
    )
    assert complete.outcome is ObservationCompleteOutcome.DEADLINE_EXPIRED
    # Only the begin transaction ran; no finalize transaction started.
    assert len(client.transactions) == 1


def test_fail_observation_skips_the_write_when_the_deadline_would_not_fit() -> None:
    # fail_observation is best effort: when a commit deadline is supplied and one
    # full sub-call would not fit before it, the terminal record is skipped
    # rather than started, leaving the operation observing for a later reclaim.
    client = StatefulDynamoClient()
    _begin(_store(client))
    transactions_before = len(client.transactions)
    _store(client).fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
        commit_not_after=NOW + timedelta(seconds=2.0),
    )
    assert len(client.transactions) == transactions_before  # no fail write started


def test_fail_observation_writes_when_the_deadline_fits() -> None:
    client = StatefulDynamoClient()
    _begin(_store(client))
    store = _store(client)
    store.fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
        commit_not_after=NOW + timedelta(seconds=15),
    )
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "failed"


def test_begin_non_conditional_error_is_provider_unavailable() -> None:
    # A non-conditional store fault (throttle/conflict) is a retryable
    # unavailable state, never a false idempotency/state 409.
    client = StatefulDynamoClient()
    client.raise_non_conditional = True
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE


# --- replay / conflict / in-progress --------------------------------------


def test_begin_replays_completed_operation_without_new_read() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    # A second begin with the same token+fingerprint resolves the completed op.
    begin = _begin(store)
    assert begin.outcome is ObservationBeginOutcome.REPLAY_COMPLETED
    assert begin.observation is not None
    assert begin.observation["observation_id"] == OPERATION_ID
    assert begin.observation_hash == canonical_sha256(_observation())


def test_begin_succeeded_replay_fails_closed_when_deadline_passes_before_loading_result() -> None:
    # On a completed replay the store reads the snapshot, then loads the result:
    # each is a sub-call. If the request budget runs out between the two reads,
    # the store must fail closed with DEADLINE_EXPIRED before starting the
    # second read, rather than racing the Lambda timeout to load the result.
    client = StatefulDynamoClient()
    _begin(_store(client))
    _complete(_store(client))

    # A clock that stays within budget for the begin-phase deadline checks and
    # the pre-snapshot check, and only advances past the (deadline - call_limit)
    # horizon on the check immediately before _load_result. Clock calls in this
    # replay path: begin top, begin pre-transact, resolve pre-snapshot, then the
    # succeeded-branch check before _load_result.
    deadline = NOW + timedelta(seconds=15)
    times = [NOW, NOW, NOW, NOW + timedelta(seconds=11)]

    def advancing() -> datetime:
        return times.pop(0) if len(times) > 1 else times[0]

    store = DynamoDbObservationStore(client=client, table_name=TABLE, clock=advancing, call_limit_s=4.5)
    begin = store.begin_observation(
        operation_id=OPERATION_ID,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=HOLDER,
        commit_not_after=deadline,
        lease_not_after=NOW + timedelta(seconds=15),
        intent=INTENT,
    )
    assert begin.outcome is ObservationBeginOutcome.DEADLINE_EXPIRED


def test_complete_transaction_fences_on_the_current_time_not_zero() -> None:
    # The finalize transaction's unexpired-lease condition compares the stored
    # lease deadline against ``:now`` (the current epoch second). A mutation that
    # sent ``:now = 0`` would make every lease look expired (0 is before any
    # real lease), silently defeating the fence. Pin the real value.
    client = StatefulDynamoClient()
    _begin(_store(client))
    _complete(_store(client))
    update = next(e for e in client.transactions[-1] if "Update" in e)["Update"]
    now_value = int(update["ExpressionAttributeValues"][":now"]["N"])
    assert now_value == int(NOW.timestamp())
    assert now_value > 0


def test_begin_changed_intent_is_idempotency_conflict() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    before = {key: dict(value) for key, value in client.items.items()}
    # Same token, different fingerprint => conflict, never mutates the op.
    conflict = store.begin_observation(
        operation_id="obs_bbbbbbbbbbbbbbbbbbbbbbbbbb",
        idempotency_fingerprint="sha256:" + "b" * 64,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder="request.observe-2",
        commit_not_after=NOW + timedelta(minutes=30),
        lease_not_after=NOW + timedelta(seconds=15),
        intent={"phase": "observe", "provider": "gamelift", "target": {"provider": "gamelift", "fleet_id": "fleet-z"}},
    )
    assert conflict.outcome is ObservationBeginOutcome.IDEMPOTENCY_CONFLICT
    # The conflict mutated nothing: no second operation, no changed item.
    assert client.items == before


def test_begin_in_progress_when_not_yet_completed() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    # Retry before completion => in progress, no second operation.
    retry = _begin(store)
    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS
    assert retry.current_state == "observing"


def test_no_scan_and_all_puts_conditional() -> None:
    client = StatefulDynamoClient()
    _begin(_store(client))
    assert not hasattr(client, "scan")
    for entry in client.transactions[0]:
        assert "Put" in entry
        assert "ConditionExpression" in entry["Put"]


# --- complete -------------------------------------------------------------


def test_complete_records_result_and_advances_state() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    complete = _complete(store)
    assert complete.outcome is ObservationCompleteOutcome.RECORDED
    result = client.items[(f"OP#{OPERATION_ID}", "RESULT#current")]
    assert result["observation_hash"]["S"] == canonical_sha256(_observation())
    stored = json.loads(result["observation_json"]["S"])
    assert stored["observation_id"] == OPERATION_ID
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "succeeded"
    transition = client.items[(f"OP#{OPERATION_ID}", "STATE#1")]
    assert transition["new_state"]["S"] == "succeeded"


def test_complete_is_append_only_on_ledger() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    items = client.transactions[-1]
    ledger = next(e for e in items if "Put" in e and e["Put"]["Item"]["SK"]["S"] == "LEDGER#1")
    assert ledger["Put"]["ConditionExpression"] == "attribute_not_exists(SK)"


def test_complete_rejects_result_above_item_size_limit() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    big = _observation()
    big["padding"] = "x" * (400 * 1024 + 10)
    with pytest.raises(ObservationStoreError):
        _complete(store, big)


def test_complete_expired_deadline_fails_closed() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    complete = store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW - timedelta(seconds=1),
        observation=_observation(),
    )
    assert complete.outcome is ObservationCompleteOutcome.DEADLINE_EXPIRED


def test_complete_under_wrong_holder_is_state_conflict() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    complete = store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder="request.someone-else",
        commit_not_after=NOW + timedelta(minutes=30),
        observation=_observation(),
    )
    assert complete.outcome is ObservationCompleteOutcome.STATE_CONFLICT


# --- fail -----------------------------------------------------------------


def test_fail_records_bounded_failed_transition() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    store.fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
    )
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "failed"
    transition = client.items[(f"OP#{OPERATION_ID}", "STATE#1")]
    assert transition["new_state"]["S"] == "failed"
    assert transition["reason_code"]["S"] == "provider_unavailable"


def test_fail_is_best_effort_and_swallows_errors() -> None:
    client = StatefulDynamoClient()
    client.raise_non_conditional = True
    store = _store(client)
    # No begin: the update condition fails; fail_observation must not raise.
    store.fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
    )


# --- status ---------------------------------------------------------------


def test_load_status_succeeded_returns_observation() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    status = store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE)
    assert status is not None
    assert status.state is ObservationStatusView.SUCCEEDED
    assert status.observation is not None
    assert status.observation_hash == canonical_sha256(_observation())


def test_load_status_observing_has_no_observation() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    status = store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE)
    assert status is not None
    assert status.state is ObservationStatusView.OBSERVING
    assert status.observation is None


def test_load_status_cross_workspace_is_invisible() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    assert store.load_status(operation_id=OPERATION_ID, workspace_id="workspace.other") is None


def test_load_status_missing_is_none() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    assert store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE) is None


def test_table_name_required() -> None:
    with pytest.raises(ValueError):
        DynamoDbObservationStore(client=StatefulDynamoClient(), table_name="  ")


# --- Adversarial: stale-lease reclaim, fencing races -----------------------


def _TransactionCanceled(*reason_codes: str) -> ClientError:
    """A real TransactionCanceledException carrying explicit cancellation reasons."""
    return _transaction_canceled(*reason_codes)


def _seed_observing(
    client: StatefulDynamoClient, *, lease_not_after: int, generation: int = 1, holder: str = HOLDER
) -> None:
    """Seed a raw observing snapshot with an explicit lease deadline/generation."""
    store = _store(client)
    store.begin_observation(
        operation_id=OPERATION_ID,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=holder,
        commit_not_after=NOW + timedelta(minutes=30),
        lease_not_after=NOW + timedelta(seconds=15),
        intent=INTENT,
    )
    snap = dict(client.items[(f"OP#{OPERATION_ID}", "STATE#current")])
    snap["lease_not_after"] = {"N": str(lease_not_after)}
    snap["generation"] = {"N": str(generation)}
    client.items[(f"OP#{OPERATION_ID}", "STATE#current")] = snap


def test_begin_reclaims_stale_lease_and_advances_generation() -> None:
    client = StatefulDynamoClient()
    # Lease already expired at NOW.
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    store = _store(client, clock=NOW)
    retry = _begin(store, operation_id="obs_" + "b" * 26)
    assert retry.outcome is ObservationBeginOutcome.RECLAIMED
    assert retry.operation_id == OPERATION_ID
    assert retry.generation == 2
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["generation"]["N"] == "2"
    assert snapshot["lease_holder"]["S"] == HOLDER  # the reclaiming caller's holder
    # A recovery ledger transition was appended, keyed to sort between the
    # create and terminal events and carrying a strictly increasing sequence.
    assert (f"OP#{OPERATION_ID}", "LEDGER#0#RECLAIM#0002") in client.items
    reclaim = client.items[(f"OP#{OPERATION_ID}", "LEDGER#0#RECLAIM#0002")]
    assert reclaim["event_type"]["S"] == "observation.lease_reclaimed"
    assert reclaim["sub_sequence"]["N"] == "2"
    # The reclaim event sorts after the create event and before the terminal one.
    assert "LEDGER#0#RECLAIM#0002" > "LEDGER#0"
    assert "LEDGER#0#RECLAIM#0002" < "LEDGER#1"
    # The reclaimed lease is the caller's own request lease, not the far commit
    # deadline, so a dead reclaimer frees the operation within one request lease.
    reclaimed_lease = int(client.items[(f"OP#{OPERATION_ID}", "STATE#current")]["lease_not_after"]["N"])
    assert reclaimed_lease <= int((NOW + timedelta(seconds=15)).timestamp())


def test_begin_live_lease_is_in_progress_not_reclaimed() -> None:
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW + timedelta(seconds=15)).timestamp()))
    store = _store(client, clock=NOW)
    retry = _begin(store, operation_id="obs_" + "b" * 26)
    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS
    # The live lease is untouched: no reclaim ledger event, generation unchanged.
    assert (f"OP#{OPERATION_ID}", "LEDGER#0#RECLAIM#0002") not in client.items
    assert client.items[(f"OP#{OPERATION_ID}", "STATE#current")]["generation"]["N"] == "1"


def test_reclaim_race_loser_conditional_failure_is_in_progress_not_duplicate() -> None:
    # A reclaimer that observed the stale generation-1 lease but lost the race
    # (a concurrent winner already advanced to generation 2) submits its fenced
    # :cur_gen=1 update, which now fails the ConditionalCheck. It must fall back
    # to in-progress — never a second operation, never a duplicate reclaim.
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    winner = _begin(_store(client, clock=NOW), operation_id="obs_" + "b" * 26)
    assert winner.outcome is ObservationBeginOutcome.RECLAIMED
    assert client.items[(f"OP#{OPERATION_ID}", "STATE#current")]["generation"]["N"] == "2"

    # The loser still holds a stale read (generation 1) and its transact write
    # is rejected with a pure ConditionalCheckFailed. Simulate the wire failure
    # its reclaim update would receive after the winner advanced the generation.
    original = client.transact_write_items

    def _reject_stale_reclaim(
        *, TransactItems: list[dict[str, Any]], ClientRequestToken: str | None = None
    ) -> dict[str, Any]:
        for entry in TransactItems:
            if "Update" in entry:
                vals = entry["Update"].get("ExpressionAttributeValues", {})
                if ":cur_gen" in vals and int(vals[":cur_gen"]["N"]) == 1:
                    raise _TransactionCanceled("ConditionalCheckFailed")
        return original(TransactItems=TransactItems, ClientRequestToken=ClientRequestToken)

    client.transact_write_items = _reject_stale_reclaim  # type: ignore[method-assign]
    # Force the loser to observe the pre-winner generation-1 lease as expired.
    snap = dict(client.items[(f"OP#{OPERATION_ID}", "STATE#current")])
    snap["generation"] = {"N": "1"}
    snap["lease_not_after"] = {"N": str(int((NOW - timedelta(seconds=1)).timestamp()))}
    client.items[(f"OP#{OPERATION_ID}", "STATE#current")] = snap
    loser = _begin(_store(client, clock=NOW), operation_id="obs_" + "c" * 26)
    assert loser.outcome is ObservationBeginOutcome.IN_PROGRESS
    # No duplicate reclaim ledger for a generation-3 was written.
    assert (f"OP#{OPERATION_ID}", "LEDGER#0#RECLAIM#0003") not in client.items


def test_complete_under_reclaimed_generation_fences_out_superseded_writer() -> None:
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    store = _store(client, clock=NOW)
    reclaim = _begin(store, operation_id="obs_" + "b" * 26)
    assert reclaim.outcome is ObservationBeginOutcome.RECLAIMED
    # The superseded writer (generation 1, original holder) is barred from committing.
    stale = store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(minutes=30),
        observation=_observation(),
        generation=1,
    )
    assert stale.outcome is ObservationCompleteOutcome.STATE_CONFLICT
    # The reclaiming writer (generation 2) commits.
    won = store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(minutes=30),
        observation=_observation(),
        generation=2,
    )
    assert won.outcome is ObservationCompleteOutcome.RECORDED
    assert client.items[(f"OP#{OPERATION_ID}", "STATE#current")]["state"]["S"] == "succeeded"


# --- Adversarial: terminal-failed replay -----------------------------------


def test_begin_replays_terminal_failure_distinctly_from_in_progress() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    store.fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
    )
    replay = _begin(store, operation_id="obs_" + "f" * 26)
    assert replay.outcome is ObservationBeginOutcome.REPLAY_FAILED
    assert replay.operation_id == OPERATION_ID
    assert replay.failure_reason == "provider_unavailable"
    # No second operation snapshot was created.
    snapshots = [k for k in client.items if k[1] == "STATE#current"]
    assert len(snapshots) == 1


# --- Adversarial: non-conditional TransactionCanceled classification --------


def test_begin_transaction_conflict_reason_is_provider_unavailable_not_409() -> None:
    client = StatefulDynamoClient()

    def _raise(**_kwargs: Any) -> None:
        raise _TransactionCanceled("TransactionConflict", "None")

    client.transact_write_items = _raise  # type: ignore[method-assign]
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE


def test_begin_throttling_reason_is_provider_unavailable() -> None:
    client = StatefulDynamoClient()

    def _raise(**_kwargs: Any) -> None:
        raise _TransactionCanceled("ThrottlingError")

    client.transact_write_items = _raise  # type: ignore[method-assign]
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE


def test_begin_mixed_conflict_and_conditional_reason_is_retryable_not_conditional() -> None:
    # If a transient conflict reason is present anywhere, the whole transaction
    # is retryable even when another leg reports ConditionalCheckFailed — it must
    # never be resolved as a 409 idempotency/state conflict.
    client = StatefulDynamoClient()

    def _raise(**_kwargs: Any) -> None:
        raise _TransactionCanceled("ConditionalCheckFailed", "TransactionConflict")

    client.transact_write_items = _raise  # type: ignore[method-assign]
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE


def test_begin_pure_conditional_reason_resolves_idempotency() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)  # seed the mapping so resolution finds an in-progress op
    # A pure ConditionalCheckFailed routes to idempotency resolution.
    retry = _begin(store, operation_id="obs_" + "b" * 26)
    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS


# --- Retention: no DynamoDB TTL on any item (ADR 0005) ---------------------


def test_begin_items_carry_no_ttl_attribute() -> None:
    # The idempotency mapping, snapshot, both transitions, and the ledger events
    # are retained for the full audit/replay window and carry no ``ttl``.
    client = StatefulDynamoClient()
    _begin(_store(client))
    for key, item in client.items.items():
        assert "ttl" not in item, f"{key} unexpectedly carries a ttl attribute"


def test_complete_items_carry_no_ttl_attribute() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    for key, item in client.items.items():
        assert "ttl" not in item, f"{key} unexpectedly carries a ttl attribute"
    # The complete snapshot update expression never sets a ttl.
    complete_tx = client.transactions[-1]
    update = next(e for e in complete_tx if "Update" in e)["Update"]
    assert "ttl" not in update["UpdateExpression"]
    assert "#ttl" not in update.get("ExpressionAttributeNames", {})


def test_fail_items_carry_no_ttl_attribute() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    store.fail_observation(
        operation_id=OPERATION_ID, workspace_id=WORKSPACE, lease_holder=HOLDER, reason_code="provider_unavailable"
    )
    for key, item in client.items.items():
        assert "ttl" not in item, f"{key} unexpectedly carries a ttl attribute"


# --- Golden fencing expressions: pin the exact conditional expressions ------
#
# These golden assertions pin the exact ConditionExpression strings so inverting
# a comparison in the complete, fail, or reclaim fence fails a test even though
# the stateful fake decides conditions from the value map.


def test_complete_condition_expression_is_pinned() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    update = next(e for e in client.transactions[-1] if "Update" in e)["Update"]
    assert update["ConditionExpression"] == (
        "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
        "AND #gen = :gen AND lease_holder = :holder AND lease_not_after > :now"
    )
    assert update["UpdateExpression"] == (
        "SET #state = :succeeded, #seq = :one, last_transition = :state1, observation_hash = :hash"
    )


def test_fail_condition_expression_is_pinned() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    store.fail_observation(
        operation_id=OPERATION_ID, workspace_id=WORKSPACE, lease_holder=HOLDER, reason_code="provider_unavailable"
    )
    update = next(e for e in client.transactions[-1] if "Update" in e)["Update"]
    assert update["ConditionExpression"] == (
        "attribute_exists(PK) AND #state = :observing AND #seq = :zero " "AND #gen = :gen AND lease_holder = :holder"
    )


def test_reclaim_condition_expression_is_pinned() -> None:
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    store = _store(client, clock=NOW)
    _begin(store, operation_id="obs_" + "b" * 26)
    reclaim_tx = client.transactions[-1]
    update = next(e for e in reclaim_tx if "Update" in e)["Update"]
    assert update["ConditionExpression"] == (
        "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
        "AND #gen = :cur_gen AND lease_not_after <= :now"
    )


def test_reclaim_update_expression_and_values_are_pinned() -> None:
    # Golden assertions on the reclaim UpdateExpression and the :now / :new_lease
    # values. The stateful fake applies updates from the value map and ignores
    # the expression text, so these pin what a lease-advancing mutation would
    # otherwise leave uncaught.
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    store = _store(client, clock=NOW)
    _begin(store, operation_id="obs_" + "b" * 26)
    update = next(e for e in client.transactions[-1] if "Update" in e)["Update"]
    assert update["UpdateExpression"] == ("SET #gen = :new_gen, lease_holder = :holder, lease_not_after = :new_lease")
    values = update["ExpressionAttributeValues"]
    # :now is the store clock in epoch seconds; the lease fence is <= :now.
    assert int(values[":now"]["N"]) == int(NOW.timestamp())
    # :new_lease is the caller's own request lease (NOW + 15 s here), not the
    # far-off commit deadline.
    assert int(values[":new_lease"]["N"]) == int((NOW + timedelta(seconds=15)).timestamp())


def test_reclaim_omits_client_request_token() -> None:
    # The reclaim transaction carries NO ClientRequestToken: competing reclaimers
    # that observed the same expired generation would otherwise derive the same
    # deterministic token with different lease/holder/now values, which DynamoDB
    # rejects as IdempotentParameterMismatch. The generation fence alone makes it
    # idempotent.
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    store = _store(client, clock=NOW)
    _begin(store, operation_id="obs_" + "b" * 26)
    # The last transaction is the reclaim; its request token is None, while the
    # seed begin carried a token.
    assert client.request_tokens[-1] is None
    assert client.request_tokens[0] is not None


def test_reclaimed_lease_is_the_callers_lease_even_with_a_far_commit_deadline() -> None:
    # When the commit deadline is far beyond the caller's request lease, the
    # reclaimed lease is the caller's own request lease, so a reclaimer that dies
    # frees the operation within one request lease rather than the whole commit
    # window.
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW - timedelta(seconds=1)).timestamp()))
    store = _store(client, clock=NOW)
    reclaim = store.begin_observation(
        operation_id="obs_" + "d" * 26,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(minutes=30),  # far beyond the lease
        lease_not_after=NOW + timedelta(seconds=15),  # the caller's request lease
        intent=INTENT,
    )
    assert reclaim.outcome is ObservationBeginOutcome.RECLAIMED
    reclaimed_lease = int(client.items[(f"OP#{OPERATION_ID}", "STATE#current")]["lease_not_after"]["N"])
    assert reclaimed_lease == int((NOW + timedelta(seconds=15)).timestamp())


def test_begin_resolution_fails_closed_when_the_deadline_passes_between_subcalls() -> None:
    # The conflict-resolution path makes several key lookups, each with its own
    # wall-clock cost. If the request deadline passes between the idempotency
    # mapping read and the snapshot read, the store fails closed with
    # DEADLINE_EXPIRED rather than issuing another sub-call past the budget.
    client = StatefulDynamoClient()
    _seed_observing(client, lease_not_after=int((NOW + timedelta(seconds=15)).timestamp()))
    # A clock that advances on each call: the mapping read happens before the
    # deadline, but the between-subcall check sees a time at/after it.
    deadline = NOW + timedelta(seconds=5)
    ticks = iter([NOW, NOW, deadline + timedelta(seconds=1), deadline + timedelta(seconds=2)])
    store = DynamoDbObservationStore(client=client, table_name=TABLE, clock=lambda: next(ticks))
    result = store.begin_observation(
        operation_id="obs_" + "e" * 26,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=HOLDER,
        commit_not_after=deadline,
        lease_not_after=deadline,
        intent=INTENT,
    )
    assert result.outcome is ObservationBeginOutcome.DEADLINE_EXPIRED


# --- Deterministic ClientRequestToken on every transaction -----------------


def test_begin_passes_deterministic_client_request_token() -> None:
    # A retried begin whose first attempt committed must not resurface as a
    # spurious conflict: the transaction carries a deterministic idempotency
    # token so botocore's transparent retry is a server-side no-op.
    client = StatefulDynamoClient()
    _begin(_store(client))
    assert client.request_tokens[0] is not None
    assert len(client.request_tokens[0]) <= 36
    # The token is stable for the same operation/phase/generation.
    client2 = StatefulDynamoClient()
    _begin(_store(client2))
    assert client.request_tokens[0] == client2.request_tokens[0]


def test_complete_and_fail_tokens_differ_by_phase() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    begin_token, complete_token = client.request_tokens[0], client.request_tokens[-1]
    assert begin_token != complete_token
