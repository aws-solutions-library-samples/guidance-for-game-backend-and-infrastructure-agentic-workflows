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
2. Make that non-disclosure assertion genuinely *discriminating*: the same
   raising code path is exercised through a ``diagnose=True`` sink to prove the
   sensitive local's VALUE would in fact be disclosed there, and through the
   production ``diagnose=False`` sink to prove it is not. Without the positive
   half, ``_SENSITIVE_LOCAL not in output`` could pass simply because the value
   never enters any traceback — proving nothing about ``diagnose``.
3. Guard against the regression at the framework level by asserting that
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


def _add_sink(buffer: io.StringIO, *, diagnose: bool) -> int:
    """Add a loguru sink; ``diagnose`` toggles local-VALUE annotation."""
    return logger.add(
        buffer,
        level="DEBUG",
        format="{level} | {name}:{function}:{line} | {message}",
        backtrace=False,
        diagnose=diagnose,
    )


class _ProductionSink:
    """A loguru sink configured exactly like the production stdout handler.

    Production uses ``backtrace=False, diagnose=False`` (see utils/logger.py):
    the traceback carries frames + exception class + message, but no local
    variable values.
    """

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._sink_id = _add_sink(self._buffer, diagnose=False)

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


class _DiagnoseSink:
    """A loguru sink with ``diagnose=True`` — annotates local VALUES.

    This is the debug/non-production configuration (utils/logger.py enables it
    only when ``_DEBUG_LOGGING``). It exists here to prove the sensitive value
    genuinely flows into the traceback, so the production sink's *absence* of it
    is a real property of ``diagnose=False`` and not an artifact of the value
    never appearing.
    """

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._sink_id = _add_sink(self._buffer, diagnose=True)

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


def _boom(_token: str) -> None:
    """Innermost frame that raises. ``_token`` is bound so the caller's
    reference to the sensitive local sits on the failing source line, which is
    what loguru's ``diagnose`` annotates."""
    raise ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "You haven't enabled historical data beyond 14 months",
            }
        },
        "GetCostAndUsage",
    )


def _raise_client_error() -> None:
    """Raise a realistic botocore error, passing a sensitive value on the
    failing call line so ``diagnose=True`` would annotate its VALUE."""
    secret_request_token = _SENSITIVE_LOCAL
    _boom(secret_request_token)


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


def test_sensitive_local_disclosure_is_discriminating():
    """The non-disclosure guarantee is a property of ``diagnose=False``.

    Exercising the identical raising path through both sinks proves the point:
    with ``diagnose=True`` the sensitive local's VALUE is annotated into the
    traceback, and with the production ``diagnose=False`` sink it is not. This is
    what makes ``_SENSITIVE_LOCAL not in output`` a real assertion rather than a
    vacuous one.
    """
    # Positive half: diagnose=True DOES disclose the local's value.
    with _DiagnoseSink() as diag_buffer:
        try:
            _raise_client_error()
        except ClientError:
            logger.exception("operation failed")
        diagnose_output = diag_buffer.getvalue()

    assert "ClientError" in diagnose_output
    assert "Traceback (most recent call last)" in diagnose_output
    assert _SENSITIVE_LOCAL in diagnose_output, (
        "expected diagnose=True to annotate the sensitive local's VALUE; if this "
        "fails the negative assertion below proves nothing"
    )

    # Negative half: the production sink (diagnose=False) does NOT disclose it,
    # while still carrying class + message + frames.
    with _ProductionSink() as prod_buffer:
        try:
            _raise_client_error()
        except ClientError:
            logger.exception("operation failed")
        production_output = prod_buffer.getvalue()

    assert "ClientError" in production_output
    assert "ValidationException" in production_output
    assert "Traceback (most recent call last)" in production_output
    assert _SENSITIVE_LOCAL not in production_output


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
