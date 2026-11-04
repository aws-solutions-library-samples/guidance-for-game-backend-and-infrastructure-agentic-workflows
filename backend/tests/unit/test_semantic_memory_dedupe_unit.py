#!/usr/bin/env python3
"""Unit tests for cross-session semantic-memory deduplication.

Each AgentCore session can run in a fresh process, so the in-process hash cache
starts empty and cannot prevent a fact stated in an earlier session from being
written again. These tests drive :func:`save_semantic_memory` with the
data-plane memory client stubbed so the store already contains (or does not
contain) a normalized-equal fact, and assert that the function consults the
store before writing, honors ``failedRecords``, and fails open when the
existence check itself errors without logging fact text or actor ids.

The existence check uses ``retrieve_memory_records`` (the same read action used
for retrieval), so no additional IAM action is required. A botocore ``Stubber``
validates request parameters and response fields against the installed
``bedrock-agentcore`` service model where practical.
"""

# Standard library
import datetime
import threading
from unittest.mock import Mock, patch

# Third-party packages
import botocore.session
import pytest
from botocore.config import Config
from botocore.stub import Stubber

# Local modules
from utils.logger import logger

pytestmark = pytest.mark.unit

_ACTOR = "actor-123"
_FACT = "User's name is John"


@pytest.fixture
def clear_dedup_cache():
    """Clear the in-process deduplication cache around each test."""
    # Local modules
    from utils import semantic_memory

    semantic_memory._saved_memory_hashes.clear()
    yield
    semantic_memory._saved_memory_hashes.clear()


def _retrieve_response(texts, namespaces=None):
    """A RetrieveMemoryRecords response listing the given record texts.

    ``namespaces`` optionally overrides the per-record namespace list (defaults
    to the queried actor) so a record stored for a different actor can be
    simulated.
    """
    record_namespaces = namespaces if namespaces is not None else [_ACTOR]
    return {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": f"mr-{i:040d}",
                "content": {"text": text},
                "memoryStrategyId": "user_facts",
                "namespaces": record_namespaces,
                "createdAt": datetime.datetime(2026, 1, 1),
            }
            for i, text in enumerate(texts)
        ]
    }


def _mock_client(retrieve_response=None, retrieve_error=None):
    """Build a Mock memory client with retrieve/batch-create behavior."""
    client = Mock()
    if retrieve_error is not None:
        client.retrieve_memory_records.side_effect = retrieve_error
    else:
        client.retrieve_memory_records.return_value = retrieve_response or {"memoryRecordSummaries": []}
    client.batch_create_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [],
    }
    return client


def test_cold_process_existing_record_skips_write(clear_dedup_cache):
    """Cold cache, store already holds a casing/whitespace variant: no write, True."""
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response(["  user's NAME is john  "]))

    def _success(req_id):
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    client.batch_create_memory_records.side_effect = lambda **kw: _success(kw["records"][0]["requestIdentifier"])

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        result = semantic_memory.save_semantic_memory(_ACTOR, _FACT)

    assert result is True
    client.batch_create_memory_records.assert_not_called()


def test_new_fact_writes_once_then_in_process_fast_path(clear_dedup_cache):
    """Store has only other facts: write once; a second same-process call skips both."""
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response(["Platform: GameLift"]))

    def _success(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    client.batch_create_memory_records.side_effect = _success

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True
        assert client.retrieve_memory_records.call_count == 1
        assert client.batch_create_memory_records.call_count == 1

        # Second call in the same process must skip both the lookup and the write.
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True
        assert client.retrieve_memory_records.call_count == 1
        assert client.batch_create_memory_records.call_count == 1


def test_failed_record_returns_false_and_retries(clear_dedup_cache):
    """A response listing the request as failed returns False and does not mark saved."""
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response([]))

    def _failed(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        return {
            "successfulRecords": [],
            "failedRecords": [
                {"memoryRecordId": "m", "status": "FAILED", "requestIdentifier": req_id, "errorCode": 500}
            ],
        }

    client.batch_create_memory_records.side_effect = _failed

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is False
        assert client.batch_create_memory_records.call_count == 1

        # Not marked saved, so the next call retries the write.
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is False
        assert client.batch_create_memory_records.call_count == 2


def test_existence_check_failure_fails_open_and_logs_class_only(clear_dedup_cache):
    """If the lookup raises, the write still happens and the fail-open line is content-free.

    This asserts only about the single fail-open log line emitted when the
    existence check errors: it is logged at WARNING, names the exception class,
    and carries no fact text or actor id. It does not inspect the surrounding
    save log lines.
    """
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_error=RuntimeError("throttled"))

    def _success(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    client.batch_create_memory_records.side_effect = _success

    captured: list = []
    sink_id = logger.add(lambda m: captured.append(m.record), level="WARNING", format="{message}")
    try:
        with (
            patch("utils.semantic_memory._get_memory_client", return_value=client),
            patch("utils.semantic_memory._get_existence_check_client", return_value=client),
            patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
        ):
            result = semantic_memory.save_semantic_memory(_ACTOR, _FACT)
    finally:
        logger.remove(sink_id)

    assert result is True
    client.batch_create_memory_records.assert_called_once()
    # Exactly the fail-open line: logged at WARNING, naming the exception class.
    failopen = [r for r in captured if r["level"].name == "WARNING" and "existence check" in r["message"].lower()]
    assert failopen, "a WARNING fail-open message must be logged when the existence check errors"
    combined = " ".join(r["message"] for r in failopen)
    assert "RuntimeError" in combined
    assert _ACTOR not in combined
    assert "John" not in combined
    assert _FACT not in combined
    assert "throttled" not in combined


def test_retrieve_params_match_service_model(clear_dedup_cache):
    """The existence check sends parameters that the service model accepts.

    A botocore Stubber validates ``retrieve_memory_records`` request parameters
    and the response shape against the installed ``bedrock-agentcore`` model,
    then the save short-circuits on the normalized-equal record it returns.
    """
    # Local modules
    from utils import semantic_memory

    # The unit-test harness patches ``boto3.client`` globally, so build the real
    # client through botocore's session to validate parameters against the
    # installed service model.
    client = botocore.session.get_session().create_client(
        "bedrock-agentcore",
        region_name="us-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(signature_version="v4"),
    )
    stubber = Stubber(client)
    stubber.add_response(
        "retrieve_memory_records",
        _retrieve_response([_FACT]),
        {
            "memoryId": "test-memory-id",
            "namespace": _ACTOR,
            "searchCriteria": {"searchQuery": _FACT, "topK": 5},
            "maxResults": 5,
        },
    )
    stubber.activate()

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        result = semantic_memory.save_semantic_memory(_ACTOR, _FACT)

    assert result is True
    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_foreign_namespace_record_does_not_suppress_write(clear_dedup_cache):
    """A normalized-equal record stored for a different actor must not skip the write.

    The service treats ``namespace`` as a prefix, so a prefix sibling (for
    example ``actor-1239`` when saving for ``actor-123``) can appear in the
    results. The existence check must only count a record whose ``namespaces``
    list contains the saving actor exactly, so a foreign-namespace record with
    identical content still writes.
    """
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response([_FACT], namespaces=["actor-1239"]))

    def _success(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    client.batch_create_memory_records.side_effect = _success

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        result = semantic_memory.save_semantic_memory(_ACTOR, _FACT)

    assert result is True
    client.batch_create_memory_records.assert_called_once()


def test_concurrent_same_fact_saves_write_once(clear_dedup_cache):
    """A second same-process save of a fact that is still being written does not write.

    The second save runs from inside the first save's batch-create call, where the
    first writer is known to be in flight, so the ordering is deterministic. A
    flag stops the nested call from recursing if the guard is missing.
    """
    # Local modules
    from utils import semantic_memory

    write_calls: list = []
    nested_results: list = []
    nested_started = {"value": False}

    def _write(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        write_calls.append(req_id)
        if not nested_started["value"]:
            nested_started["value"] = True
            worker = threading.Thread(
                target=lambda: nested_results.append(semantic_memory.save_semantic_memory(_ACTOR, _FACT))
            )
            worker.start()
            worker.join(10)
            assert not worker.is_alive(), "nested save did not finish"
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    client = _mock_client(retrieve_response=_retrieve_response([]))
    client.batch_create_memory_records.side_effect = _write

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        first = semantic_memory.save_semantic_memory(_ACTOR, _FACT)

    assert first is True
    assert nested_results == [True]
    assert len(write_calls) == 1


def test_claim_rechecks_saved_cache(clear_dedup_cache):
    """A caller that passed the fast path before another caller marked the fact does not write.

    The fast-path check and the in-flight claim take the lock separately. The
    claim re-checks the saved cache, so a fact marked in between is not written
    again even when the store lookup would miss.
    """
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response([]))
    semantic_memory._mark_memory_saved(_ACTOR, _FACT)

    with (
        patch("utils.semantic_memory._is_duplicate_memory", return_value=False),
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True

    client.retrieve_memory_records.assert_not_called()
    client.batch_create_memory_records.assert_not_called()


@pytest.mark.parametrize(
    ("stored_namespace", "expect_write"),
    [
        pytest.param("/actor-123/", False, id="slash-delimited-own"),
        pytest.param("/actor-1239/", True, id="slash-delimited-sibling"),
        pytest.param("/actor-123/child/", True, id="child-path"),
    ],
)
def test_namespace_forms_are_compared_normalized(clear_dedup_cache, stored_namespace, expect_write):
    """The stored namespace is compared without leading or trailing slashes."""
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response([_FACT], namespaces=[stored_namespace]))

    def _success(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    client.batch_create_memory_records.side_effect = _success

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True

    assert client.batch_create_memory_records.call_count == (1 if expect_write else 0)


def test_lookup_uses_existence_client_and_write_uses_write_client(clear_dedup_cache):
    """The existence check reads through the short-timeout client; the write uses the write client."""
    # Local modules
    from utils import semantic_memory

    lookup_client = _mock_client(retrieve_response=_retrieve_response([]))
    write_client = _mock_client()

    def _success(**kw):
        req_id = kw["records"][0]["requestIdentifier"]
        return {
            "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": req_id}],
            "failedRecords": [],
        }

    write_client.batch_create_memory_records.side_effect = _success

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=write_client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=lookup_client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True

    lookup_client.retrieve_memory_records.assert_called_once()
    lookup_client.batch_create_memory_records.assert_not_called()
    write_client.retrieve_memory_records.assert_not_called()
    write_client.batch_create_memory_records.assert_called_once()


def test_write_failure_log_omits_exception_message(clear_dedup_cache):
    """A failed write logs the exception class name, not its message."""
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response([]))
    client.batch_create_memory_records.side_effect = RuntimeError("record text Platform: GameLift for actor-123")

    captured: list = []
    sink_id = logger.add(lambda m: captured.append(m.record), level="ERROR", format="{message}")
    try:
        with (
            patch("utils.semantic_memory._get_memory_client", return_value=client),
            patch("utils.semantic_memory._get_existence_check_client", return_value=client),
            patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
        ):
            assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is False
    finally:
        logger.remove(sink_id)

    combined = " ".join(r["message"] for r in captured)
    assert "RuntimeError" in combined
    assert "record text" not in combined
    assert _ACTOR not in combined


def test_store_hit_marks_cache_so_second_call_skips_lookup(clear_dedup_cache):
    """A store-hit path marks the cache: a second same-process call skips the lookup."""
    # Local modules
    from utils import semantic_memory

    client = _mock_client(retrieve_response=_retrieve_response([_FACT]))

    with (
        patch("utils.semantic_memory._get_memory_client", return_value=client),
        patch("utils.semantic_memory._get_existence_check_client", return_value=client),
        patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"),
    ):
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True
        assert client.retrieve_memory_records.call_count == 1
        client.batch_create_memory_records.assert_not_called()

        # Second call hits the in-process fast path: no further lookup or write.
        assert semantic_memory.save_semantic_memory(_ACTOR, _FACT) is True
        assert client.retrieve_memory_records.call_count == 1
        client.batch_create_memory_records.assert_not_called()


def test_existence_check_uses_dedicated_short_timeout_client():
    """The existence check uses its own cached client with a short-timeout config.

    The config carries a connect timeout, a read timeout, and one total attempt,
    and it differs from the main write client's config so a degraded existence
    read cannot hold the response path for the default read timeout.
    """
    # Local modules
    from utils import semantic_memory

    cfg = semantic_memory._EXISTENCE_CHECK_CLIENT_CONFIG
    assert cfg.connect_timeout == 3
    assert cfg.read_timeout == 5
    real = botocore.session.get_session().create_client(
        "bedrock-agentcore",
        region_name="us-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=cfg,
    )
    # One total attempt: botocore resolves the configured retries to this value.
    assert real.meta.config.retries["total_max_attempts"] == 1

    created: list = []

    def _fake_boto3_client(service, **kwargs):
        created.append(kwargs.get("config"))
        return Mock()

    with patch("utils.semantic_memory.boto3.client", side_effect=_fake_boto3_client):
        semantic_memory._reset_clients_for_test()
        first = semantic_memory._get_existence_check_client()
        second = semantic_memory._get_existence_check_client()

    assert first is second  # cached like the write client
    assert created[0] is cfg
    semantic_memory._reset_clients_for_test()


def test_write_confirmed_empty_success_list_is_unconfirmed():
    """An empty ``successfulRecords`` is not a confirmed write."""
    # Local modules
    from utils import semantic_memory

    response = {"successfulRecords": [], "failedRecords": []}
    assert semantic_memory._write_confirmed(response, "req-1") is False


def test_write_confirmed_identifier_omitted_is_confirmed():
    """A non-empty success list with no echoed identifier and no failure is confirmed."""
    # Local modules
    from utils import semantic_memory

    response = {"successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED"}], "failedRecords": []}
    assert semantic_memory._write_confirmed(response, "req-1") is True


def test_write_confirmed_other_identifier_only_is_unconfirmed():
    """A success list that echoes identifiers but omits this request's is unconfirmed."""
    # Local modules
    from utils import semantic_memory

    response = {
        "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": "other"}],
        "failedRecords": [],
    }
    assert semantic_memory._write_confirmed(response, "req-1") is False


def test_write_confirmed_request_listed_failed_is_unconfirmed():
    """A request listed in ``failedRecords`` is unconfirmed even if a success is present."""
    # Local modules
    from utils import semantic_memory

    response = {
        "successfulRecords": [{"memoryRecordId": "m", "status": "SUCCEEDED", "requestIdentifier": "req-1"}],
        "failedRecords": [{"memoryRecordId": "n", "status": "FAILED", "requestIdentifier": "req-1"}],
    }
    assert semantic_memory._write_confirmed(response, "req-1") is False
