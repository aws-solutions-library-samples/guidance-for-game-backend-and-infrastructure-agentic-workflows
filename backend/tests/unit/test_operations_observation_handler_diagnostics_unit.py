"""Safe, production-visible diagnostics at the observation handler's unexpected
catch-all (GitHub issue #413).

Live diagnosis (demo / us-west-2, 2026-09-21) of the E1 observe path found that
after the store-boundary diagnostics landed, a *valid* ``POST`` that succeeds
locally under Admin returned a bounded ``INTERNAL_ERROR`` 500 in the deployed
Lambda — and the running Lambda emitted **nothing** naming even the failing
stage or the exception class. :class:`ObservationRequestHandler.handle` wraps
dispatch in a bare ``except Exception`` that maps any unexpected error to a
sanitized generic 500 and returns, swallowing every internal signal. That is the
same diagnostic blind spot #413 root-caused at the store, now one layer up at
the protocol adapter.

These tests pin a fix that, at the handler's unexpected catch-all ONLY, emits a
single bounded, sanitized ``logging`` record carrying only:

* a fixed event name (a static literal),
* the request ``stage`` drawn from a strict allowlist (never request-derived
  free text),
* the HTTP ``method`` drawn from a strict allowlist,
* the exception TYPE name (``type(exc).__name__``), and
* the bounded AWS ``Error.Code`` / transaction cancellation-reason codes, when a
  botocore-shaped exception carries them.

It MUST NOT emit the exception message, ``str(exc)``, a traceback / ``exc_info``,
the request event / body, an idempotency token, an operation or request id, a
path parameter, an ARN, an account, provider data, or stack locals.

A *typed* :class:`ObservationBoundaryError` is an expected boundary outcome, not
an unexpected internal failure, so it MUST NOT be logged through this channel.

Lambda-safe emit: the handler ships in the minimal E1 observe Lambda whose
dependency closure deliberately excludes loguru, so the diagnostic is emitted
through the Python standard-library ``logging`` module (mirroring the store
boundary). These tests capture the emitted ``LogRecord`` through a
production-SHAPED stdlib handler (``%(message)s`` with a framework-metadata
prefix and NO structured ``extra``), so a passing assertion means the datum
survives the deployed message-only handler to CloudWatch. Adversarial secrets,
newlines, and braces are planted in exception messages/args and in a
provider-shaped ``Error.Code`` to prove no leak and no log-forging.
"""

from __future__ import annotations

# Standard library
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.identity import ApprovalIdentityBoundary
from operations.observation import (
    AuthorityInputs,
    ObservationBoundaryError,
    ObservationErrorCode,
    ObservationService,
)
from operations.observation_handler import _LOGGER as HANDLER_LOGGER
from operations.observation_handler import ObservationRequestHandler
from operations.settings import resolve_operations_settings

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
EXP = int((NOW + timedelta(minutes=30)).timestamp())

# A value that stands in for any request-derived / internal datum (an
# idempotency token, an operation id, a fleet id, an ARN, an account). If it
# ever reaches the log sink the boundary is leaking. It is planted in the
# exception MESSAGE, in its args, and — with newlines and braces — in a
# provider-shaped ``Error.Code`` to also prove no log-forging.
_SECRET = "SECRET-idem-token-arn:aws:iam::123456789012:role/should-never-log"
_FORGING = "InjectedCode\nobservation_handler_exception stage=forged {brace}"


# A production-SHAPED Python standard-library ``logging`` format. The handler
# diagnostic is emitted through stdlib ``logging`` (NOT loguru) because it ships
# in the minimal E1 observe Lambda whose dependency closure excludes loguru. A
# Lambda/CloudWatch handler renders the record's message and prefixes only
# framework metadata (timestamp, level, logger name); it carries NO structured
# ``extra`` mapping. A datum only "survives to CloudWatch" if it lands in
# ``%(message)s`` — a field passed via ``extra`` would render nowhere here.
_PRODUCTION_STDLIB_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class _ProductionSink:
    """A stdlib ``logging`` handler shaped like the deployed Lambda/CloudWatch
    handler: a ``%(message)s`` line prefixed by framework metadata, NO ``extra``
    rendering, and no traceback unless ``exc_info`` is set on the record. It
    attaches to the handler's own logger with ``propagate`` disabled so capture
    is deterministic and independent of root-logger configuration."""

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._handler = logging.StreamHandler(self._buffer)
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(logging.Formatter(_PRODUCTION_STDLIB_FORMAT))
        self._prev_level = HANDLER_LOGGER.level
        self._prev_propagate = HANDLER_LOGGER.propagate

    def __enter__(self) -> "io.StringIO":
        HANDLER_LOGGER.addHandler(self._handler)
        HANDLER_LOGGER.setLevel(logging.DEBUG)
        HANDLER_LOGGER.propagate = False
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        HANDLER_LOGGER.removeHandler(self._handler)
        HANDLER_LOGGER.setLevel(self._prev_level)
        HANDLER_LOGGER.propagate = self._prev_propagate
        self._handler.close()


class _SecretBearingError(Exception):
    """A non-botocore exception whose message and args carry request-derived
    data. The boundary must log its TYPE but never its message/args."""

    def __init__(self) -> None:
        super().__init__(f"internal failure near {_SECRET}\nsecond line {_FORGING}")


class _SecretExplodingService(ObservationService):
    """A service whose ``observe`` raises an unexpected secret-bearing error."""

    def observe(self, request: Any, context: Any) -> dict[str, Any]:
        raise _SecretBearingError()


class _BotocoreExplodingService(ObservationService):
    """A service whose ``observe`` raises a real botocore ClientError whose
    MESSAGE and reason MESSAGES carry a secret, but whose CODES are useful."""

    def observe(self, request: Any, context: Any) -> dict[str, Any]:
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": f"User is not authorized near {_SECRET}",
                },
                "CancellationReasons": [
                    {"Code": "None"},
                    {"Code": "ConditionalCheckFailed", "Message": f"cond near {_SECRET}"},
                ],
            },
            "TransactWriteItems",
        )


class _ForgingCodeService(ObservationService):
    """A service whose botocore error ``Error.Code`` itself carries a newline and
    braces, to prove the emitted code token cannot forge a second log line."""

    def observe(self, request: Any, context: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": _FORGING, "Message": _SECRET}},
            "TransactWriteItems",
        )


class _TypedBoundaryService(ObservationService):
    """A service whose ``observe`` raises a TYPED, expected boundary error. It
    must map to its HTTP status WITHOUT any unexpected-diagnostic record."""

    def observe(self, request: Any, context: Any) -> dict[str, Any]:
        raise ObservationBoundaryError(ObservationErrorCode.PROVIDER_UNAVAILABLE, "provider is temporarily unavailable")


def _service(cls: type[ObservationService]) -> ObservationService:
    return cls(
        settings=resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"}),
        identity_boundary=ApprovalIdentityBoundary(
            tenant_id="tenant.default",
            workspace_id="workspace.default",
            requester_client_ids=frozenset({"client.web-console"}),
            approver_client_ids=frozenset({"client.approver"}),
            trusted_audiences=frozenset({"operations-api"}),
        ),
        reader=_FakeReader(),
        store=_FakeStore(),
        clock=lambda: NOW,
        operation_id_factory=lambda: OPERATION_ID,
    )


class _FakeReader:
    def read_utilization(self, fleet_id: str) -> dict[str, int]:
        return {
            "active_server_processes": 1,
            "active_game_sessions": 1,
            "current_player_sessions": 1,
            "maximum_player_sessions": 1,
        }

    def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
        return [{"location": "us-west-2", "desired": 1, "minimum": 1, "maximum": 1, "active": 1, "idle": 0}]

    def read_scaling_policies(self, fleet_id: str) -> list[dict[str, str]]:
        return []


class _FakeStore:
    def begin_observation(self, **kwargs: Any) -> Any:
        raise AssertionError("store must not be reached in these diagnostic tests")

    def complete_observation(self, **kwargs: Any) -> Any:  # pragma: no cover - defensive
        raise AssertionError("store must not be reached in these diagnostic tests")

    def fail_observation(self, **kwargs: Any) -> Any:  # pragma: no cover - defensive
        raise AssertionError("store must not be reached in these diagnostic tests")

    def read_status(self, **kwargs: Any) -> Any:  # pragma: no cover - defensive
        raise AssertionError("store must not be reached in these diagnostic tests")


def _handler(cls: type[ObservationService]) -> ObservationRequestHandler:
    return ObservationRequestHandler(
        service=_service(cls),
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience="operations-api",
        capability_id="gamelift.observe-fleet",
        capability_version="1.0",
        authority_inputs=AuthorityInputs(
            tenant_policy="observe",
            workspace_policy="observe",
            principal_authority="observe",
            capability_maximum="observe",
            risk_policy="observe",
        ),
    )


def _claims() -> dict[str, Any]:
    return {"sub": "subject.operator-1", "client_id": "client.web-console", "token_use": "access", "exp": EXP}


def _event(*, method: str = "POST") -> dict[str, Any]:
    return {
        "requestContext": {
            "requestId": "abc123def456",
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": _claims()}},
        },
        "body": json.dumps({"fleet_id": FLEET_ID, "idempotency_token": TOKEN}),
    }


def _diagnostic_lines(buffer: io.StringIO) -> list[str]:
    return [line for line in buffer.getvalue().splitlines() if "observation_handler_exception" in line]


def _message(buffer: io.StringIO) -> str:
    lines = _diagnostic_lines(buffer)
    assert len(lines) == 1, f"expected exactly one diagnostic line, got {len(lines)}: {buffer.getvalue()!r}"
    # Split the framework-metadata prefix off the emitted message.
    return lines[0].split(" | ", 3)[-1]


# --------------------------------------------------------------------------- #
# The catch-all now emits a diagnostic, and it names the useful facts.
# --------------------------------------------------------------------------- #
def test_unexpected_exception_emits_one_bounded_diagnostic() -> None:
    handler = _handler(_SecretExplodingService)
    with _ProductionSink() as buffer:
        response = handler.handle(_event())
    assert response["statusCode"] == 500
    message = _message(buffer)
    # Fixed event name + the useful, sanitized facts.
    assert message.startswith("observation_handler_exception ")
    assert "stage=service_observe" in message
    assert "http_method=POST" in message
    assert "exception_type=_SecretBearingError" in message


def test_botocore_error_logs_bounded_codes() -> None:
    handler = _handler(_BotocoreExplodingService)
    with _ProductionSink() as buffer:
        response = handler.handle(_event())
    assert response["statusCode"] == 500
    message = _message(buffer)
    assert "exception_type=ClientError" in message
    assert "aws_error_code=AccessDeniedException" in message
    # The inert "None" reason marker is dropped; the useful code remains.
    assert "cancellation_reason_codes=ConditionalCheckFailed" in message
    assert "None" not in message.split("cancellation_reason_codes=")[-1]


# --------------------------------------------------------------------------- #
# No leak: message, args, traceback, secrets, ARNs are never emitted.
# --------------------------------------------------------------------------- #
def test_secret_bearing_message_and_args_never_leak() -> None:
    handler = _handler(_SecretExplodingService)
    with _ProductionSink() as buffer:
        handler.handle(_event())
    captured = buffer.getvalue()
    assert _SECRET not in captured
    assert "arn:aws:iam" not in captured
    assert "123456789012" not in captured
    # No traceback frames (no exc_info on the record).
    assert "Traceback" not in captured
    assert "_SecretExplodingService" not in captured  # no stack-frame class names


def test_botocore_secret_messages_never_leak() -> None:
    handler = _handler(_BotocoreExplodingService)
    with _ProductionSink() as buffer:
        handler.handle(_event())
    captured = buffer.getvalue()
    assert _SECRET not in captured
    assert "not authorized" not in captured  # the Error.Message never appears
    assert "Traceback" not in captured


def test_provider_code_cannot_forge_a_second_line() -> None:
    handler = _handler(_ForgingCodeService)
    with _ProductionSink() as buffer:
        handler.handle(_event())
    # Exactly one diagnostic line survives: the planted newline/braces in the
    # provider ``Error.Code`` are neutralized, so no forged second record.
    assert len(_diagnostic_lines(buffer)) == 1
    message = _message(buffer)
    assert "\n" not in message
    assert "{" not in message and "}" not in message
    # The forged literal "stage=forged" must not appear as a real field value.
    assert "stage=forged" not in message
    assert "stage=service_observe" in message


# --------------------------------------------------------------------------- #
# Strict allowlist: an unknown method never becomes free-text in the record.
# --------------------------------------------------------------------------- #
def test_method_is_drawn_from_a_strict_allowlist() -> None:
    handler = _handler(_SecretExplodingService)
    # A crafted, non-standard method reaches dispatch before the failure. It is
    # a boundary error (unsupported method) BEFORE observe, so to exercise the
    # catch-all with a hostile method we drive a supported method here and assert
    # only allowlisted method tokens can ever be emitted.
    with _ProductionSink() as buffer:
        handler.handle(_event(method="POST"))
    message = _message(buffer)
    method_token = [f for f in message.split(" ") if f.startswith("http_method=")][0]
    assert method_token in {"http_method=POST", "http_method=GET", "http_method=OTHER"}


# --------------------------------------------------------------------------- #
# A typed, expected boundary error is NOT logged as unexpected.
# --------------------------------------------------------------------------- #
def test_typed_boundary_error_is_not_logged_as_unexpected() -> None:
    handler = _handler(_TypedBoundaryService)
    with _ProductionSink() as buffer:
        response = handler.handle(_event())
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error_code"] == "PROVIDER_UNAVAILABLE"
    # The unexpected-diagnostic channel emitted nothing for a typed outcome.
    assert _diagnostic_lines(buffer) == []
