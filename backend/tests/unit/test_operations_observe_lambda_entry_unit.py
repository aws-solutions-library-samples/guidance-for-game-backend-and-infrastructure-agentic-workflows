"""Unit tests for the frozen E1 observation Lambda entrypoint (issue #413).

The entrypoint is the infrastructure-owned packaging seam
``operations.observe.lambda_entry.handler``. These tests prove it honors the
``GBAW_OPERATIONS_MODE`` kill switch, fails closed when no core observation
callable is registered, and delegates only when both conditions hold.
"""

# Standard library
import importlib
import json

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def lambda_entry(monkeypatch):
    # Import fresh and reset the module-private observer between tests.
    module = importlib.import_module("operations.observe.lambda_entry")
    monkeypatch.setattr(module, "_observer", None, raising=False)
    return module


def _body(response):
    return json.loads(response["body"])


def test_disabled_mode_fails_closed(lambda_entry, monkeypatch):
    monkeypatch.delenv("GBAW_OPERATIONS_MODE", raising=False)
    response = lambda_entry.handler({}, None)
    assert response["statusCode"] == 503
    assert _body(response)["error"] == "OPERATIONS_DISABLED"


def test_non_observe_mode_fails_closed(lambda_entry, monkeypatch):
    monkeypatch.setenv("GBAW_OPERATIONS_MODE", "enabled")
    response = lambda_entry.handler({}, None)
    assert response["statusCode"] == 503
    assert _body(response)["error"] == "OPERATIONS_DISABLED"


def test_observe_mode_without_observer_fails_closed_retryable(lambda_entry, monkeypatch):
    monkeypatch.setenv("GBAW_OPERATIONS_MODE", "observe")
    response = lambda_entry.handler({}, None)
    assert response["statusCode"] == 503
    body = _body(response)
    assert body["error"] == "OBSERVER_UNAVAILABLE"
    assert body["retryable"] is True


def test_observe_mode_delegates_to_registered_observer(lambda_entry, monkeypatch):
    monkeypatch.setenv("GBAW_OPERATIONS_MODE", "observe")
    sentinel = {"statusCode": 200, "headers": {}, "body": "{}"}
    calls = []

    def _observer(event, context):
        calls.append((event, context))
        return sentinel

    lambda_entry.register_observer(_observer)
    event = {"routeKey": "POST /operations/observe"}
    response = lambda_entry.handler(event, "ctx")
    assert response is sentinel
    assert calls == [(event, "ctx")]


def test_error_bodies_leak_no_provider_detail(lambda_entry, monkeypatch):
    monkeypatch.setenv("GBAW_OPERATIONS_MODE", "observe")
    response = lambda_entry.handler({"fleetId": "fleet-secret", "arn": "arn:secret"}, None)
    # Fail-closed body must not echo any input/provider detail.
    assert "fleet-secret" not in response["body"]
    assert "arn:secret" not in response["body"]
