"""Unit tests for correct loguru exception logging (#409).

loguru does not implement the stdlib ``logging`` keyword ``exc_info=``. When an
``except`` block calls ``logger.error("...", exc_info=True)`` the keyword is
consumed only as a ``str.format`` argument and the traceback is silently
dropped, so a CloudWatch record for a failed request carries a one-line message
with no exception class, message, or stack — exactly the blind spot that hid the
Cost Explorer ``ValidationException`` observed on the 2026-09-11 demo.

The correct pattern inside an ``except`` block is ``logger.exception(msg)`` (or
``logger.opt(exception=True).error(msg)``); outside an active exception context
use ``logger.opt(exception=exc)``. Either way the emitted traceback must carry
the exception class and message but — with ``diagnose=False`` in production
(#152) — must NOT carry local variable VALUES.

These tests:

1. Prove the (now fixed) production call sites emit the exception class and
   message through a captured loguru sink configured exactly like production
   (``backtrace=False, diagnose=False``), while NOT leaking sensitive locals.
2. Guard against the regression at the framework level by asserting that
   ``exc_info=`` on a production-shaped loguru sink drops the traceback (the
   bug), so the value of switching to ``logger.exception`` is pinned by a test.
"""

# Standard library
import io

# Third-party packages
import pytest
from botocore.exceptions import ClientError
from loguru import logger

# Local modules
from agents.cost_report import CostReportError, CostReportService

pytestmark = pytest.mark.unit


# A value that, if diagnose were on, would appear as an annotated local in the
# traceback. It must never reach a production-shaped sink.
_SENSITIVE_LOCAL = "SECRET-token-value-should-never-be-logged"


class _ProductionSink:
    """A loguru sink configured exactly like the production stdout handler.

    Production uses ``backtrace=False, diagnose=False`` (see utils/logger.py):
    the traceback carries frames + exception class + message, but no local
    variable values.
    """

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._sink_id = logger.add(
            self._buffer,
            level="DEBUG",
            format="{level} | {name}:{function}:{line} | {message}",
            backtrace=False,
            diagnose=False,
        )

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


def _raise_client_error() -> None:
    """Raise a realistic botocore error with a sensitive value in a local."""
    secret_request_token = _SENSITIVE_LOCAL  # noqa: F841 -- present to prove it is not logged
    raise ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "You haven't enabled historical data beyond 14 months",
            }
        },
        "GetCostAndUsage",
    )


def _failing_client_factory():
    class _Client:
        def get_cost_and_usage(self, **_kwargs):
            _raise_client_error()

    return _Client()


def test_create_report_emits_exception_class_and_message():
    """Acceptance: a forced botocore error in create_report emits a record with
    the exception class and message (not just the one-line summary)."""
    service = CostReportService(client_factory=_failing_client_factory)

    with _ProductionSink() as buffer:
        with pytest.raises(CostReportError) as excinfo:
            service.create_report("2026-09-01", "2026-09-07")

        output = buffer.getvalue()

    # The typed error is still raised and remains sanitized for the caller.
    assert excinfo.value.code == "COST_EXPLORER_REQUEST_FAILED"
    assert "historical data" not in str(excinfo.value)

    # The server-side log now carries the underlying exception class and message.
    assert "ClientError" in output
    assert "ValidationException" in output
    assert "You haven't enabled historical data beyond 14 months" in output
    # And a traceback (frames) is present.
    assert "Traceback (most recent call last)" in output

    # But it must NOT leak local variable VALUES (diagnose=False).
    assert _SENSITIVE_LOCAL not in output


def test_production_sink_records_exception_via_logger_exception():
    """logger.exception on a production-shaped sink carries class+message+frames
    but never local variable values."""
    with _ProductionSink() as buffer:
        try:
            _raise_client_error()
        except ClientError:
            logger.exception("operation failed")
        output = buffer.getvalue()

    assert "ClientError" in output
    assert "ValidationException" in output
    assert "Traceback (most recent call last)" in output
    assert _SENSITIVE_LOCAL not in output


def test_exc_info_kwarg_drops_traceback_on_production_sink():
    """Regression pin: exc_info=True is NOT understood by loguru and drops the
    traceback entirely. This is the #409 bug; it justifies logger.exception."""
    with _ProductionSink() as buffer:
        try:
            _raise_client_error()
        except ClientError:
            logger.error("operation failed", exc_info=True)
        output = buffer.getvalue()

    # The message is present but the exception is completely absent.
    assert "operation failed" in output
    assert "Traceback (most recent call last)" not in output
    assert "ValidationException" not in output
