"""Behavioural tests for the OPTIONAL E3 execution Lambda entrypoints (issue
#415).

The 07 stack references two deployable handler modules:
``operations.execute.dispatcher_entry.handler`` and
``operations.execute.executor_entry.handler``. This slice DEFINES those frozen
entrypoints and their fail-closed kill-switch contract; the E3 core consumes and
extends the execution business logic in a later slice.

These tests never call AWS. They import the entrypoints with an injected
environment and assert:

* Both entrypoints resolve a frozen env contract and fail closed when the
  injected ``GBAW_OPERATIONS_EXECUTION_MODE`` is not ``remediate`` (lever 2 of
  the reversible emergency disable) — WITHOUT importing boto3 clients or
  attempting any provider write.
* The executor treats the state-machine payload as ``operation_id`` ONLY: a
  payload carrying a fleet id / capacity number is rejected (the executor
  re-loads those from the table under operation_id; they never cross the wire).
* Neither module performs a provider write on the disabled path.
"""

# Standard library
import importlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


DISABLED_ENV = {
    "GBAW_OPERATIONS_EXECUTION_MODE": "disabled",
    "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
    "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
    "GBAW_OPERATIONS_STATE_MACHINE_ARN": "arn:aws:states:us-west-2:123456789012:stateMachine:game-agent-operations-execution",
}


def _executor():
    module = importlib.import_module("operations.execute.executor_entry")
    return importlib.reload(module)


def _dispatcher():
    module = importlib.import_module("operations.execute.dispatcher_entry")
    return importlib.reload(module)


# --------------------------------------------------------------------------- #
# Modules import as data (no AWS at import time)
# --------------------------------------------------------------------------- #
def test_execute_package_imports():
    pkg = importlib.import_module("operations.execute")
    assert pkg is not None


def test_executor_and_dispatcher_expose_a_handler():
    assert callable(_executor().handler)
    assert callable(_dispatcher().handler)


# --------------------------------------------------------------------------- #
# Lever 2: fail closed when disabled
# --------------------------------------------------------------------------- #
def test_executor_fails_closed_when_disabled():
    mod = _executor()
    result = mod.handle_event(
        {"operation_id": "op-123"},
        env=DISABLED_ENV,
    )
    assert result["outcome"] == "denied"
    assert "disabled" in result["reason"].lower()


def test_dispatcher_fails_closed_when_disabled():
    mod = _dispatcher()
    event = {
        "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {}}}},
        "pathParameters": {"operationId": "op-123"},
    }
    response = mod.handle_event(event, env=DISABLED_ENV)
    assert response["statusCode"] in (403, 503)
    assert "disabled" in str(response.get("body", "")).lower()


# --------------------------------------------------------------------------- #
# operation_id-only contract at the executor
# --------------------------------------------------------------------------- #
def test_executor_rejects_payload_with_extra_fields():
    """The executor accepts a payload of operation_id ONLY. A payload that also
    carries fleet id / capacity numbers is rejected — those are re-loaded from
    the table, never threaded through the wire."""
    mod = _executor()
    remediate_env = dict(DISABLED_ENV, GBAW_OPERATIONS_EXECUTION_MODE="remediate")
    result = mod.handle_event(
        {"operation_id": "op-123", "fleet_id": "fleet-x", "desired_instances": 1},
        env=remediate_env,
    )
    assert result["outcome"] in ("denied", "rejected")


def test_executor_requires_operation_id():
    mod = _executor()
    remediate_env = dict(DISABLED_ENV, GBAW_OPERATIONS_EXECUTION_MODE="remediate")
    result = mod.handle_event({}, env=remediate_env)
    assert result["outcome"] in ("denied", "rejected")


# --------------------------------------------------------------------------- #
# No provider-write surface in the disabled path
# --------------------------------------------------------------------------- #
def test_disabled_executor_does_not_touch_boto3(monkeypatch):
    """On the disabled (fail-closed) path the executor must not construct any
    boto3 client — it denies before any AWS call."""
    # Standard library
    import sys

    mod = _executor()
    created = []

    class _Boom:
        def client(self, *a, **k):  # pragma: no cover - must never run
            created.append(a)
            raise AssertionError("boto3 client must not be created on the disabled path")

    monkeypatch.setitem(sys.modules, "boto3", _Boom())
    result = mod.handle_event({"operation_id": "op-123"}, env=DISABLED_ENV)
    assert result["outcome"] == "denied"
    assert not created
