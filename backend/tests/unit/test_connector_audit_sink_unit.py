#!/usr/bin/env python3
"""Unit tests for the durable, confirmed audit sink (`connector.audit.AuditSink`).

These tests exercise the sink against an in-memory fake CloudWatch Logs client (injected via
``client=``) so no AWS call occurs. They cover the confirmed-write contract (Req 13.2), the
never-raise / fail-closed guarantee (Req 13.3), the sequence-token recovery + single retry,
create-log-stream tolerance of an existing stream, and the defense-in-depth field
sanitization.
"""

# Standard library
import json

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from connector.audit import AuditSink

pytestmark = pytest.mark.unit


def _client_error(code: str, **extra) -> ClientError:
    """Build a botocore ClientError carrying ``code`` (and optional top-level fields)."""
    response = {"Error": {"Code": code, "Message": code}}
    response.update(extra)
    return ClientError(response, "PutLogEvents")


class _FakeLogsClient:
    """Minimal in-memory stand-in for the boto3 ``logs`` client."""

    def __init__(self):
        self.created_streams: list[dict] = []
        self.put_calls: list[dict] = []
        self.describe_calls: list[dict] = []
        self.create_error: Exception | None = None
        self.put_outcomes: list = []  # each entry: a response dict or an Exception to raise
        self.describe_token: str | None = None

    def create_log_stream(self, **kwargs):
        if self.create_error is not None:
            raise self.create_error
        self.created_streams.append(kwargs)

    def put_log_events(self, **kwargs):
        self.put_calls.append(kwargs)
        outcome = self.put_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def describe_log_streams(self, **kwargs):
        self.describe_calls.append(kwargs)
        return {
            "logStreams": [
                {
                    "logStreamName": kwargs["logStreamNamePrefix"],
                    "uploadSequenceToken": self.describe_token,
                }
            ]
        }


_EVENT = {"event": "scm_proposal", "message": "Change proposal created", "outcome": "created"}


def test_confirmed_write_returns_true_and_ensures_stream():
    client = _FakeLogsClient()
    client.put_outcomes = [{"nextSequenceToken": "1"}]
    sink = AuditSink("audit-grp", client=client)

    assert sink.write(_EVENT) is True
    # The stream was created (once) and exactly one put was issued with a ms timestamp.
    assert len(client.created_streams) == 1
    assert len(client.put_calls) == 1
    log_event = client.put_calls[0]["logEvents"][0]
    assert isinstance(log_event["timestamp"], int)
    assert client.put_calls[0]["logGroupName"] == "audit-grp"


def test_rejected_events_are_unconfirmed():
    client = _FakeLogsClient()
    client.put_outcomes = [
        {"nextSequenceToken": "1", "rejectedLogEventsInfo": {"tooOldLogEventEndIndex": 0}}
    ]
    sink = AuditSink("audit-grp", client=client)
    assert sink.write(_EVENT) is False


def test_missing_next_sequence_token_is_unconfirmed():
    client = _FakeLogsClient()
    client.put_outcomes = [{}]
    sink = AuditSink("audit-grp", client=client)
    assert sink.write(_EVENT) is False


def test_put_client_error_returns_false_and_never_raises():
    client = _FakeLogsClient()
    client.put_outcomes = [_client_error("ThrottlingException")]
    sink = AuditSink("audit-grp", client=client)
    assert sink.write(_EVENT) is False


def test_unexpected_exception_returns_false_and_never_raises():
    client = _FakeLogsClient()
    client.put_outcomes = [RuntimeError("boom")]
    sink = AuditSink("audit-grp", client=client)
    assert sink.write(_EVENT) is False


def test_invalid_sequence_token_refreshes_from_exception_and_retries_once():
    client = _FakeLogsClient()
    client.put_outcomes = [
        _client_error("InvalidSequenceTokenException", expectedSequenceToken="99"),
        {"nextSequenceToken": "100"},
    ]
    sink = AuditSink("audit-grp", client=client)

    assert sink.write(_EVENT) is True
    # The retry used the expected token carried by the exception.
    assert len(client.put_calls) == 2
    assert client.put_calls[1].get("sequenceToken") == "99"


def test_data_already_accepted_refreshes_via_describe_when_no_token_on_exception():
    client = _FakeLogsClient()
    client.describe_token = "77"
    client.put_outcomes = [
        _client_error("DataAlreadyAcceptedException"),
        {"nextSequenceToken": "78"},
    ]
    sink = AuditSink("audit-grp", client=client)

    assert sink.write(_EVENT) is True
    assert client.describe_calls  # fell back to describe_log_streams
    assert client.put_calls[1].get("sequenceToken") == "77"


def test_sequence_recovery_retries_only_once():
    client = _FakeLogsClient()
    # Two consecutive invalid-token errors: the single retry is consumed, so the second
    # failure is not retried again and the write is unconfirmed.
    client.put_outcomes = [
        _client_error("InvalidSequenceTokenException", expectedSequenceToken="1"),
        _client_error("InvalidSequenceTokenException", expectedSequenceToken="2"),
    ]
    sink = AuditSink("audit-grp", client=client)
    assert sink.write(_EVENT) is False
    assert len(client.put_calls) == 2


def test_create_log_stream_already_exists_is_tolerated():
    client = _FakeLogsClient()
    client.create_error = _client_error("ResourceAlreadyExistsException")
    client.put_outcomes = [{"nextSequenceToken": "1"}]
    sink = AuditSink("audit-grp", client=client)
    assert sink.write(_EVENT) is True


def test_empty_log_group_fails_closed():
    client = _FakeLogsClient()
    sink = AuditSink("", client=client)
    assert sink.write(_EVENT) is False
    assert not client.put_calls


def test_confirmed_write_caches_and_chains_sequence_token():
    client = _FakeLogsClient()
    client.put_outcomes = [{"nextSequenceToken": "5"}, {"nextSequenceToken": "6"}]
    sink = AuditSink("audit-grp", client=client)

    assert sink.write(_EVENT) is True
    assert sink.write(_EVENT) is True
    # The second put chained the token returned by the first confirmed write.
    assert "sequenceToken" not in client.put_calls[0]
    assert client.put_calls[1].get("sequenceToken") == "5"
    # The stream is only created once across repeated writes.
    assert len(client.created_streams) == 1


def test_string_fields_are_sanitized_and_truncated():
    client = _FakeLogsClient()
    client.put_outcomes = [{"nextSequenceToken": "1"}]
    sink = AuditSink("audit-grp", client=client)

    long_value = "x" * 500
    assert sink.write({"event": "scm_proposal", "big": long_value}) is True

    message = client.put_calls[0]["logEvents"][0]["message"]
    payload = json.loads(message)
    # sanitize_log_data truncates values longer than its max length with a trailing ellipsis.
    assert payload["big"].endswith("...")
    assert long_value not in message
