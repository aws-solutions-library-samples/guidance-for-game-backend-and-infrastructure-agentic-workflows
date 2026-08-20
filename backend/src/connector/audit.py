"""Durable, confirmed audit sink for the Source Control Connector (Req 13.1, 13.2, 13.3).

The baseline connector wrote its audit trail through the shared ``utils.logger.logger``
(stdout → ADOT/CloudWatch), a fire-and-forget path where a lost stdout line was never
observed. This module replaces that with a **confirmed** durable write to a dedicated
CloudWatch Logs log group.

The connector records two first-class event shapes through this sink (Req 9.1, 9.2), both
serialized and confirmed identically by :meth:`AuditSink.write`:

- **INTENT** (``event="scm_intent"``) — written *before* the first mutating provider op. It
  captures what the connector is about to attempt: the stable ``idempotency_key``, the
  ``requesting_user``, the effective ``repository``/``target_branch``, the verified
  ``base_revision``, and the proposed file ``paths``. It never carries file contents or
  secrets.
- **OUTCOME** (``event="scm_outcome"``) — written *after* the provider ops resolve (or after
  reconciliation). It carries the same ``idempotency_key`` (so it correlates with the INTENT),
  an ``outcome`` of ``created`` / ``declined`` / ``rejected`` / ``error`` / ``reconciled``,
  the ``proposal_id``/``proposal_url`` on success or a ``reason`` on failure. Rejection and
  decline paths — which perform no mutation — emit a single OUTCOME with no preceding INTENT.

The sink itself is neutral to the event shape: it treats every event as an opaque dict to
serialize and durably confirm. Crucially, the connector makes **no cross-system atomicity
claim** between this durable audit store and the provider: a confirmed INTENT gates the *start*
of a mutation (a safe pre-mutation abort if unconfirmed), while an unconfirmed OUTCOME after a
successful mutation is surfaced as a reconcilable result rather than a rollback (Req 9.2).

:class:`AuditSink` owns a boto3 ``logs`` client (built with the platform's
``BOTO3_CLIENT_CONFIG``/``AWS_REGION`` convention, matching ``utils.secrets`` and the other
modules) and exposes a single :meth:`AuditSink.write` operation:

- It serializes the audit ``event`` dict to a JSON string (every string field passed through
  :func:`utils.security.sanitize_log_data` as defense-in-depth — the connector already
  excludes the credential value entirely, Req 6.6).
- It ensures the log stream exists (``create_log_stream``, tolerating
  ``ResourceAlreadyExistsException``), then calls ``put_log_events`` with a millisecond
  timestamp and the serialized message.
- A write is treated as **confirmed** (returns ``True``) only when ``put_log_events`` returns
  a response dict that carries a ``nextSequenceToken`` and does not report
  ``rejectedLogEventsInfo`` — the real success shape of the API (Req 13.2). Any other
  response is unconfirmed and yields ``False``.
- CloudWatch's optimistic-concurrency ``InvalidSequenceTokenException`` /
  ``DataAlreadyAcceptedException`` are handled by refreshing the expected sequence token
  (from the exception payload, falling back to ``describe_log_streams``) and retrying the
  put exactly **once**.
- :meth:`write` **never raises** — every boto3/``ClientError``/unexpected exception is caught
  and reported as ``False`` so the caller (``connector.service._record_intent`` /
  ``_record_outcome``) observes an unconfirmed write: an unconfirmed INTENT aborts before any
  mutation and an unconfirmed OUTCOME after a successful mutation yields a reconcilable result
  (Req 13.3, 9.2).

The log stream name is deterministic per process **per UTC date** (``scm-audit-<YYYYMMDD>``)
so entries accumulate in a small, predictable set of streams and the daily rollover keeps any
single stream from growing without bound. The sequence token returned by each successful put
is cached on the instance (keyed by stream name) so consecutive writes chain correctly without
a describe call.
"""

from __future__ import annotations

# Standard library
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

# Third-party packages
from botocore.exceptions import ClientError

# Local modules
from utils.security import sanitize_log_data

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Third-party packages
    from mypy_boto3_logs import CloudWatchLogsClient

__all__ = ["AuditSink"]

# Log-stream naming: one deterministic stream per process per UTC date. Keeping the stream
# name predictable (rather than random per write) means audit entries land in a small, easy
# to locate set of streams; the daily suffix bounds the size of any single stream.
_STREAM_PREFIX = "scm-audit"

# CloudWatch Logs error codes we recover from by refreshing the expected sequence token and
# retrying the put exactly once (optimistic-concurrency on the per-stream token).
_SEQUENCE_RECOVERABLE = frozenset({"InvalidSequenceTokenException", "DataAlreadyAcceptedException"})


class AuditSink:
    """Writes connector audit entries to a dedicated CloudWatch Logs group (Req 13.1, 13.2).

    A write is confirmed only when ``put_log_events`` returns a response carrying a
    ``nextSequenceToken`` with no ``rejectedLogEventsInfo``; any exception or unconfirmed
    response makes :meth:`write` return ``False`` so the caller can abort the action
    (Req 13.3). The instance lazily builds a boto3 ``logs`` client unless one is injected
    (for tests).
    """

    def __init__(
        self,
        log_group: str,
        *,
        region: str | None = None,
        client: "CloudWatchLogsClient | None" = None,
    ) -> None:
        self._log_group = log_group
        self._region = region
        self._client = client
        # Per-stream cached upload sequence token (CloudWatch's optimistic-concurrency token).
        self._sequence_tokens: dict[str, str | None] = {}
        # Streams we have already attempted to create this process, to avoid a create call
        # on every write.
        self._created_streams: set[str] = set()

    # -- client / stream helpers --------------------------------------------------------

    def _get_client(self) -> "CloudWatchLogsClient":
        """Return the injected client or lazily build a boto3 ``logs`` client.

        Uses the same convention as the rest of the backend (``utils.secrets``): the
        platform ``BOTO3_CLIENT_CONFIG`` (adaptive retries) and the configured
        ``AWS_REGION``, with an explicit ``region`` override when supplied.
        """
        if self._client is None:
            # Third-party packages
            import boto3

            # Local modules
            from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG

            self._client = boto3.client(
                "logs",
                region_name=self._region or AWS_REGION,
                config=BOTO3_CLIENT_CONFIG,
            )
        return self._client

    def _stream_name(self) -> str:
        """Return the deterministic per-process, per-UTC-date log stream name."""
        utcdate = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"{_STREAM_PREFIX}-{utcdate}"

    def _ensure_stream(self, client: "CloudWatchLogsClient", stream: str) -> None:
        """Best-effort create of ``stream``, tolerating an already-existing stream.

        Any create failure is swallowed here: if the stream genuinely cannot be created the
        subsequent ``put_log_events`` fails and :meth:`write` reports ``False``. We never let
        this raise so the confirmed-write contract is decided solely by the put response.
        """
        if stream in self._created_streams:
            return
        try:
            client.create_log_stream(logGroupName=self._log_group, logStreamName=stream)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "ResourceAlreadyExistsException":
                # Leave uncreated; the put will surface any real problem as an unconfirmed
                # write. Do not raise.
                return
        except Exception:  # noqa: BLE001 - never raise from the audit path
            return
        self._created_streams.add(stream)

    # -- serialization ------------------------------------------------------------------

    def _serialize(self, event: dict[str, Any]) -> str:
        """Serialize ``event`` to a compact JSON string with string fields sanitized.

        Every string value (and string element of a list/tuple value) is passed through
        :func:`sanitize_log_data` as defense-in-depth so no secret can leak into the audit
        record; the connector already excludes the credential value from the event entirely
        (Req 6.6).
        """
        safe: dict[str, Any] = {}
        for key, value in event.items():
            if isinstance(value, str):
                safe[key] = sanitize_log_data(value)
            elif isinstance(value, (list, tuple)):
                safe[key] = [sanitize_log_data(item) if isinstance(item, str) else item for item in value]
            else:
                safe[key] = value
        return json.dumps(safe, default=str, sort_keys=True)

    # -- sequence-token handling --------------------------------------------------------

    def _refresh_sequence_token(
        self,
        client: "CloudWatchLogsClient",
        stream: str,
        exc: ClientError | None,
    ) -> None:
        """Refresh the cached expected sequence token for ``stream``.

        Prefers the ``expectedSequenceToken`` carried by the raised exception; falls back to
        a ``describe_log_streams`` lookup when the exception does not carry one.
        """
        token: str | None = None
        if exc is not None:
            token = exc.response.get("expectedSequenceToken")
        if token is None:
            token = self._describe_sequence_token(client, stream)
        self._sequence_tokens[stream] = token

    def _describe_sequence_token(self, client: "CloudWatchLogsClient", stream: str) -> str | None:
        """Look up the current upload sequence token for ``stream`` via describe."""
        try:
            response = client.describe_log_streams(
                logGroupName=self._log_group,
                logStreamNamePrefix=stream,
                limit=1,
            )
        except Exception:  # noqa: BLE001 - never raise from the audit path
            return None
        for entry in response.get("logStreams", []):
            if entry.get("logStreamName") == stream:
                return cast("str | None", entry.get("uploadSequenceToken"))
        return None

    def _confirm(self, response: Any, stream: str) -> bool:
        """Return ``True`` only for a confirmed ``put_log_events`` response (Req 13.2).

        The real success shape carries a ``nextSequenceToken`` and no
        ``rejectedLogEventsInfo``. On confirmation the returned token is cached so the next
        write chains without a describe. Anything else (missing token, rejected events, a
        non-dict / empty response) is unconfirmed.
        """
        if not isinstance(response, dict):
            return False
        if response.get("rejectedLogEventsInfo"):
            return False
        next_token = response.get("nextSequenceToken")
        if next_token is None:
            return False
        self._sequence_tokens[stream] = next_token
        return True

    def _put(
        self,
        client: "CloudWatchLogsClient",
        stream: str,
        log_event: dict[str, Any],
        *,
        allow_retry: bool,
    ) -> bool:
        """Call ``put_log_events`` once, recovering the sequence token and retrying once."""
        kwargs: dict[str, Any] = {
            "logGroupName": self._log_group,
            "logStreamName": stream,
            "logEvents": [log_event],
        }
        token = self._sequence_tokens.get(stream)
        if token is not None:
            kwargs["sequenceToken"] = token
        try:
            response = client.put_log_events(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if allow_retry and code in _SEQUENCE_RECOVERABLE:
                self._refresh_sequence_token(client, stream, exc)
                return self._put(client, stream, log_event, allow_retry=False)
            return False
        except Exception:  # noqa: BLE001 - never raise from the audit path
            return False
        return self._confirm(response, stream)

    # -- public API ---------------------------------------------------------------------

    def write(self, event: dict[str, Any]) -> bool:
        """Durably write one audit ``event``; return ``True`` only on a confirmed write.

        Serializes the event, ensures the log stream exists, and puts the log event with a
        millisecond timestamp. Returns ``True`` only when CloudWatch Logs confirms the write
        (see :meth:`_confirm`). Never raises: any failure — serialization, missing log group,
        client construction, or a boto3 error — yields ``False`` so the caller observes an
        unconfirmed write (an unconfirmed INTENT aborts before any mutation; an unconfirmed
        OUTCOME after a successful mutation yields a reconcilable result — Req 13.3, 9.2).
        """
        if not self._log_group:
            return False
        try:
            message = self._serialize(event)
            client = self._get_client()
            stream = self._stream_name()
            self._ensure_stream(client, stream)
            log_event = {"timestamp": int(time.time() * 1000), "message": message}
            return self._put(client, stream, log_event, allow_retry=True)
        except Exception:  # noqa: BLE001 - the audit path must never raise to the caller
            return False
