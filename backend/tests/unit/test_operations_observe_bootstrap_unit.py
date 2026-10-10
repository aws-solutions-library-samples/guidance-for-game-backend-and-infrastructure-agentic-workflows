"""Unit tests for the frozen deployment contract and Lambda bootstrap (#413)."""

from __future__ import annotations

# Standard library
import importlib
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.settings import resolve_observation_deployment_settings

_COMPLETE_ENV = {
    "GBAW_OPERATIONS_MODE": "observe",
    "GBAW_OPERATIONS_TABLE_NAME": "operations-observations",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "operations-api",
    "GBAW_OPERATIONS_PER_READ_BUDGET_S": "3.0",
    "GBAW_OPERATIONS_PERSISTENCE_BUDGET_S": "3.0",
    "GBAW_OPERATIONS_CANCELLATION_MARGIN_S": "3.0",
    "GBAW_OPERATIONS_OBSERVATION_TTL_S": "1800",
}


def test_deployment_settings_resolve_the_full_frozen_contract() -> None:
    settings = resolve_observation_deployment_settings(env=_COMPLETE_ENV)
    assert settings.mode == "observe"
    assert settings.table_name == "operations-observations"
    assert settings.metric_namespace == "GBAW/Operations"
    assert settings.tenant_id == "tenant.default"
    assert settings.workspace_id == "workspace.default"
    assert settings.trusted_audience == "operations-api"
    assert settings.operations.per_read_budget_s == 3.0
    assert settings.observe_enabled is True


@pytest.mark.parametrize(
    "missing",
    [
        "GBAW_OPERATIONS_TABLE_NAME",
        "GBAW_OPERATIONS_METRIC_NAMESPACE",
        "GBAW_OPERATIONS_TENANT_ID",
        "GBAW_OPERATIONS_WORKSPACE_ID",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
    ],
)
def test_missing_required_value_fails_closed(missing: str) -> None:
    env = {k: v for k, v in _COMPLETE_ENV.items() if k != missing}
    with pytest.raises(ValueError):
        resolve_observation_deployment_settings(env=env)


@pytest.mark.parametrize("mode", ["disabled", "observe", "advise", "remediate", "operate"])
def test_all_frozen_modes_are_accepted(mode: str) -> None:
    env = dict(_COMPLETE_ENV, GBAW_OPERATIONS_MODE=mode)
    settings = resolve_observation_deployment_settings(env=env)
    assert settings.mode == mode


def test_invalid_mode_fails_closed() -> None:
    env = dict(_COMPLETE_ENV, GBAW_OPERATIONS_MODE="superuser")
    with pytest.raises(ValueError):
        resolve_observation_deployment_settings(env=env)


def test_invalid_tenant_identifier_fails_closed() -> None:
    env = dict(_COMPLETE_ENV, GBAW_OPERATIONS_TENANT_ID="a b")
    with pytest.raises(ValueError):
        resolve_observation_deployment_settings(env=env)


# --- Module import / bootstrap -------------------------------------------


def test_lambda_module_imports_and_exposes_module_level_handler() -> None:
    module = importlib.import_module("operations.observe.lambda_entry")
    assert callable(module.handler)
    # No content-bucket assumption anywhere in the deployable module source.
    # Standard library
    import inspect

    source = inspect.getsource(module)
    lowered = source.lower()
    assert "bucket" not in lowered
    assert "s3" not in lowered


def test_bootstrap_builds_handler_over_injected_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local modules
    import operations.observe.lambda_entry as entry

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.created: list[str] = []

        def client(self, name: str, **kwargs: Any) -> Any:
            return object()

    for key, value in _COMPLETE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.Session", FakeSession, raising=False)
    monkeypatch.setattr(entry, "_region", lambda: "us-west-2")
    entry._handler.cache_clear()
    handler, metrics = entry._handler()
    assert hasattr(handler, "handle")
    # The metrics sink is returned alongside the handler for per-request latency.
    assert metrics is not None
    # The latency-publish floor is wired from the frozen budgets: the total
    # deadline bounds the request and the persistence budget is the floor below
    # which the synchronous publish is skipped. A mutation dropping these
    # arguments would leave the sink with no floor and is caught here.
    settings = resolve_observation_deployment_settings(env=_COMPLETE_ENV)
    assert metrics._total_deadline_s == settings.operations.total_deadline_s
    assert metrics._min_publish_remaining_s == settings.operations.persistence_budget_s
    # The store's deadline guard uses one full persistence sub-call limit
    # (connect + read on the single-attempt client).
    persistence_s = settings.operations.persistence_budget_s
    expected_call_limit = min(2.0, max(0.5, persistence_s / 2.0)) + persistence_s
    assert handler._service._store._call_limit_s == expected_call_limit
    entry._handler.cache_clear()


def test_bounded_config_uses_persistence_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local modules
    import operations.observe.lambda_entry as entry

    settings = resolve_observation_deployment_settings(env=_COMPLETE_ENV)
    # Store/metrics clients derive their timeouts from the persistence budget
    # and make a single attempt so no transparent SDK retry outlasts the budget.
    config = entry._bounded_config(settings)
    assert config.read_timeout == settings.operations.persistence_budget_s
    assert config.connect_timeout <= 2.0
    assert config.retries["max_attempts"] == 1
    # The GameLift read client derives its timeout from the per-read budget.
    read_config = entry._read_config(settings)
    assert read_config.read_timeout == settings.operations.per_read_budget_s
    assert read_config.retries["max_attempts"] == 1
