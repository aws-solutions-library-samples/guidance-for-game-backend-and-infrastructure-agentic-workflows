"""Durable window-state seed API for the E5 autonomy deploy wrapper (issue #440).

The autonomy reservation store reads the rolling-window snapshot at
``PK=AUTZ#<state_id>, SK=AUTZWINDOW`` but never *creates* it: the runtime only
advances an already-present snapshot behind a revision + ``in_flight`` fence.
The initial snapshot is a deploy-time seed, and #440's ``--enable`` wrapper is
the only writer allowed to create it.

These tests pin the seed's safety properties with red-green coverage:

* it validates the document against the frozen #438 window-state contract before
  any write, so a malformed or hash-inconsistent snapshot is refused;
* it writes with a conditional ``attribute_not_exists(SK)`` put, so it can never
  clobber a live snapshot the runtime is already fencing on;
* an identical re-seed is idempotent (a benign re-run of the wrapper), while a
  *different* snapshot for the same ``state_id`` is refused without overwrite;
* the persisted item carries the denormalized ``state_revision`` / ``in_flight``
  the reserve fence reads, and a ``current()`` round-trip returns the exact
  document; and
* a transient/throttle fault fails closed as an error, never a silent success.
"""

from __future__ import annotations

# Standard library
import json
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime.store import (
    DynamoDbReservationStore,
    ReservationStoreError,
)
from operations.contracts import load_json

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"


def _window_state() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")


def _conditional_failure() -> Exception:
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "PutItem",
    )


def _throttle() -> Exception:
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ThrottlingException"}}, "PutItem")


class _FakeDynamo:
    """Minimal fake modeling a conditional ``PutItem`` + ``GetItem``."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_next: Exception | None = None
        self.puts = 0

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        self.puts += 1
        item = kwargs["Item"]
        key = (item["PK"]["S"], item["SK"]["S"])
        cond = kwargs.get("ConditionExpression")
        if cond == "attribute_not_exists(SK)" and key in self.items:
            raise _conditional_failure()
        self.items[key] = item
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {"Item": item} if item is not None else {}


def _store(fake: _FakeDynamo) -> DynamoDbReservationStore:
    return DynamoDbReservationStore(client=fake, table_name="game-agent-operations")


def test_seed_then_current_round_trips_the_exact_window_state() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    window = _window_state()

    store.seed_window_state(window)

    assert store.current(window["state_id"]) == window


def test_seed_persists_denormalized_revision_and_in_flight_for_the_fence() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    window = _window_state()

    store.seed_window_state(window)

    item = fake.items[(f"AUTZ#{window['state_id']}", "AUTZWINDOW")]
    assert item["state_revision"] == {"N": str(window["state_revision"])}
    assert item["in_flight"] == {"N": str(window["in_flight"])}
    # The canonical document is stored under `document` exactly.
    assert json.loads(item["document"]["S"]) == window


def test_seed_uses_conditional_attribute_not_exists_put() -> None:
    fake = _FakeDynamo()
    store = _store(fake)

    captured: dict[str, Any] = {}
    original = fake.put_item

    def _spy(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return original(**kwargs)

    fake.put_item = _spy  # type: ignore[method-assign]
    store.seed_window_state(_window_state())

    assert captured["ConditionExpression"] == "attribute_not_exists(SK)"


def test_identical_reseed_is_idempotent() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    window = _window_state()

    store.seed_window_state(window)
    # A benign re-run of the deploy wrapper.
    store.seed_window_state(window)

    assert store.current(window["state_id"]) == window


def test_reseed_of_a_different_state_is_refused_without_overwrite() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    window = _window_state()
    store.seed_window_state(window)

    drifted = _window_state()
    drifted["state_revision"] = window["state_revision"] + 1

    with pytest.raises(ReservationStoreError):
        store.seed_window_state(drifted)

    # The originally sealed snapshot is untouched.
    assert store.current(window["state_id"]) == window


def test_seed_refuses_a_malformed_window_state_before_any_write() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    window = _window_state()
    window.pop("state_revision")

    with pytest.raises(ReservationStoreError):
        store.seed_window_state(window)

    assert fake.puts == 0


def test_seed_fails_closed_on_transient_fault() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    fake.fail_next = _throttle()

    with pytest.raises(ReservationStoreError):
        store.seed_window_state(_window_state())
