"""Behavioural tests for the deployable E3 execution Lambda entrypoints (issue
#415), validated against the REAL core handlers.

The 07 stack references two deployable handler modules:
``operations.execute.dispatcher_entry.handler`` and
``operations.execute.executor_entry.handler``. These are the real, deployable
E3 core handlers (the infra slice used to ship thin placeholder seams under
``operations.execute.settings`` / ``handle_event``; those have been deleted and
this suite now validates the real handlers directly, so a combined checkout has
no add/add conflict and no divergent second implementation).

These tests never call AWS. They import the entrypoints with an injected
environment (``os.environ`` via monkeypatch, since the real handlers resolve the
frozen contract through
:func:`operations.settings.resolve_executor_deployment_settings`) and assert:

* Both entrypoints expose a ``handler(event, context)`` and import side-effect
  free (no settings resolution or boto3 client at import time).
* The executor resolves the frozen env contract and FAILS CLOSED when the
  injected ``GBAW_OPERATIONS_MODE`` / ``GBAW_OPERATIONS_CAPABILITY_MAXIMUM`` is
  below ``remediate`` (lever 2 of the reversible emergency disable) WITHOUT
  constructing any boto3 client or attempting a provider write.
* The executor treats the state-machine payload as ``operation_id`` ONLY: a
  payload carrying a fleet id / capacity number is rejected (the executor
  re-loads those from the table under operation_id; they never cross the wire).
* Neither module performs a provider write on the disabled path.
"""

from __future__ import annotations

# Standard library
import importlib
import os
import sys

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


# The full frozen E3 deployment contract the real core settings resolver
# requires. Mode is the single backend-enforced ceiling: ``disabled`` (or any
# value below ``remediate``) is fail-closed; only ``remediate`` admits a write.
_FLEET_ID = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
_FLEET_ARN = f"arn:aws:gamelift:us-west-2:123456789012:fleet/{_FLEET_ID}"

BASE_ENV = {
    "AWS_REGION": "us-west-2",
    "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "client-abc123",
    "GBAW_OPERATIONS_STATE_MACHINE_ARN": (
        "arn:aws:states:us-west-2:123456789012:stateMachine:game-agent-operations-execution"
    ),
    "GBAW_OPERATIONS_ENROLLED_FLEET_ID": _FLEET_ID,
    "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": _FLEET_ARN,
    "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
    "GBAW_OPERATIONS_APPROVER_GROUP": "admin",
    "GBAW_OPERATIONS_CAPABILITY_MAXIMUM": "remediate",
    "GBAW_OPERATIONS_CAPACITY_FLOOR": "0",
    "GBAW_OPERATIONS_CAPACITY_CEILING": "1",
    "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "1",
}

DISABLED_ENV = dict(BASE_ENV, GBAW_OPERATIONS_MODE="disabled")
REMEDIATE_ENV = dict(BASE_ENV, GBAW_OPERATIONS_MODE="remediate")


class _Boom:
    """A boto3 stand-in that fails loudly if any client/session is constructed."""

    def client(self, *a, **k):  # pragma: no cover - must never run on fail-closed paths
        raise AssertionError("boto3 client must not be created on the fail-closed path")

    def Session(self, *a, **k):  # pragma: no cover
        raise AssertionError("boto3 Session must not be created on the fail-closed path")


def _apply_env(monkeypatch, env):
    """Set exactly the injected contract, clearing any ambient GBAW_OPERATIONS_*."""
    for key in [k for k in os.environ if k.startswith("GBAW_OPERATIONS_")]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _fresh(module_name):
    """Import (or reload) a handler module with its per-container cache cleared."""
    module = importlib.import_module(module_name)
    module = importlib.reload(module)
    # The real handlers memoize the built runtime/handler with lru_cache; clear
    # it so each test resolves the currently-injected environment.
    for attr in ("_runtime", "_handler"):
        cached = getattr(module, attr, None)
        if cached is not None and hasattr(cached, "cache_clear"):
            cached.cache_clear()
    return module


def _executor():
    return _fresh("operations.execute.executor_entry")


def _dispatcher():
    return _fresh("operations.execute.dispatcher_entry")


# --------------------------------------------------------------------------- #
# Modules import as data (no AWS, no settings resolution at import time)
# --------------------------------------------------------------------------- #
def test_execute_package_imports():
    pkg = importlib.import_module("operations.execute")
    assert pkg is not None


def test_executor_and_dispatcher_expose_a_handler():
    assert callable(_executor().handler)
    assert callable(_dispatcher().handler)


def test_import_does_not_construct_boto3(monkeypatch):
    """Importing either entry must not build a boto3 client (settings + clients
    resolve lazily on first invocation, not at import)."""
    monkeypatch.setitem(sys.modules, "boto3", _Boom())
    assert callable(_executor().handler)
    assert callable(_dispatcher().handler)


# --------------------------------------------------------------------------- #
# Lever 2: the executor fails closed when the deployment ceiling is below
# remediate — before constructing any boto3 client or issuing any write.
# --------------------------------------------------------------------------- #
def test_executor_fails_closed_when_disabled(monkeypatch):
    _apply_env(monkeypatch, DISABLED_ENV)
    mod = _executor()
    monkeypatch.setitem(sys.modules, "boto3", _Boom())
    with pytest.raises(RuntimeError) as excinfo:
        mod.handler({"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}, None)
    message = str(excinfo.value).lower()
    assert "remediate" in message or "enabled" in message


def test_executor_fails_closed_when_capability_below_remediate(monkeypatch):
    """Even at deployment mode ``remediate`` a capability maximum below
    ``remediate`` must fail closed (both levers must admit the write)."""
    _apply_env(monkeypatch, dict(REMEDIATE_ENV, GBAW_OPERATIONS_CAPABILITY_MAXIMUM="advise"))
    mod = _executor()
    monkeypatch.setitem(sys.modules, "boto3", _Boom())
    with pytest.raises(RuntimeError):
        mod.handler({"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}, None)


def test_disabled_executor_does_not_touch_boto3(monkeypatch):
    """On the disabled (fail-closed) path the executor must not construct any
    boto3 client — it denies before any AWS call."""
    _apply_env(monkeypatch, DISABLED_ENV)
    mod = _executor()
    monkeypatch.setitem(sys.modules, "boto3", _Boom())
    with pytest.raises(RuntimeError):
        mod.handler({"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}, None)


def test_misconfigured_env_fails_closed_at_resolution(monkeypatch):
    """A missing required binding (no enrolled fleet ARN) fails closed at
    settings resolution, before any client is built."""
    broken = dict(REMEDIATE_ENV)
    broken.pop("GBAW_OPERATIONS_ENROLLED_FLEET_ARN")
    _apply_env(monkeypatch, broken)
    mod = _executor()
    monkeypatch.setitem(sys.modules, "boto3", _Boom())
    with pytest.raises((ValueError, RuntimeError)):
        mod.handler({"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}, None)


# --------------------------------------------------------------------------- #
# operation_id-only contract at the executor
# --------------------------------------------------------------------------- #
def test_executor_rejects_payload_with_extra_fields(monkeypatch):
    """The executor accepts a payload of operation_id ONLY. A payload that also
    carries fleet id / capacity numbers is rejected — those are re-loaded from
    the table, never threaded through the wire."""
    _apply_env(monkeypatch, REMEDIATE_ENV)
    _executor()

    # Local modules
    from operations.execute.executor_service import ExecutionInvocation, ExecutorServiceError

    with pytest.raises(ExecutorServiceError):
        ExecutionInvocation.from_payload(
            {"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa", "fleet_id": "fleet-x", "desired_instances": 1}
        )


def test_executor_requires_operation_id(monkeypatch):
    _apply_env(monkeypatch, REMEDIATE_ENV)
    _executor()

    # Local modules
    from operations.execute.executor_service import ExecutionInvocation, ExecutorServiceError

    with pytest.raises(ExecutorServiceError):
        ExecutionInvocation.from_payload({})


def test_executor_accepts_bare_operation_id(monkeypatch):
    _apply_env(monkeypatch, REMEDIATE_ENV)
    _executor()

    # Local modules
    from operations.execute.executor_service import ExecutionInvocation

    invocation = ExecutionInvocation.from_payload({"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"})
    assert invocation.operation_id == "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
