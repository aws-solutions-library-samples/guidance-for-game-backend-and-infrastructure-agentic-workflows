#!/usr/bin/env python3
"""The connector's active audit config is scoped to exactly one read invocation.

The ContextVar token returned by ``set`` must be reset after every success, early return,
and exception. Exact token restoration also makes nested reads return to the outer audit
config and keeps concurrent task/thread contexts independent.

Validates: PR #411 ContextVar lifecycle review.
"""

# Standard library
import asyncio
import threading
from typing import Any

# Third-party packages
import pytest

# Local modules
import utils.security as security
from connector import service as service_module
from connector.config import AllowlistEntry, SourceControlConfig
from connector.models import FileContent, FileFetchResult
from connector.provider import ProviderError
from connector.service import read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"
_GROUP = "scm-writers"


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> bool:
        with self._lock:
            self.events.append(event)
        return True


class _AuditRouter:
    """Config-sensitive audit lookup that never touches CloudWatch or the sink cache."""

    def __init__(self) -> None:
        self.sinks: dict[str, _Sink] = {}
        self.observed_groups: list[str] = []
        self._lock = threading.Lock()

    def sink_for(self, group: str) -> _Sink:
        with self._lock:
            return self.sinks.setdefault(group, _Sink())

    def lookup(self, config: SourceControlConfig | None) -> _Sink | None:
        if config is None or config.connector.audit_log_group is None:
            return None
        group = config.connector.audit_log_group
        with self._lock:
            self.observed_groups.append(group)
            return self.sinks.setdefault(group, _Sink())


@pytest.fixture
def audit_router(monkeypatch) -> _AuditRouter:
    router = _AuditRouter()
    monkeypatch.setattr(service_module, "_get_audit_sink", router.lookup)
    security._rate_limit_windows.clear()
    return router


def _config(group: str) -> SourceControlConfig:
    return make_source_control_config(
        enabled=True,
        read_credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf",
        allowlist=[AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,))],
        authorized_groups=[_GROUP],
        rate_limit_max=1000,
        audit_log_group=group,
    )


def _context(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "groups": [_GROUP],
        "tenant": "acme",
        "workspace": "prod",
    }


def _served(path: str) -> FileFetchResult:
    return FileFetchResult(
        files=(FileContent(path=path, content="resource {}"),),
        missing=(),
        limit_exceeded=False,
    )


def _event_paths(sink: _Sink) -> list[list[str]]:
    return [event["normalized_paths"] for event in sink.events if event.get("outcome") == "served"]


def test_active_config_restored_after_success(audit_router):
    previous = _config("/audit/previous")
    current = _config("/audit/current")
    reader = FakeProvider().program("get_files", returns=_served("main.tf"))
    previous_token = service_module._active_config.set(previous)
    request_token = set_request_context(_context("reader-success"))
    try:
        result = read_iac_files(["main.tf"], config=current, reader=reader)

        assert result.files[0].path == "main.tf"
        assert service_module._active_config.get() is previous
        assert audit_router.observed_groups == ["/audit/current"]
        assert _event_paths(audit_router.sink_for("/audit/current")) == [["main.tf"]]
    finally:
        reset_request_context(request_token)
        service_module._active_config.reset(previous_token)


def test_active_config_restored_after_provider_error(audit_router):
    previous = _config("/audit/previous")
    current = _config("/audit/current")
    reader = FakeProvider().fail("get_files", ProviderError("provider failed"))
    previous_token = service_module._active_config.set(previous)
    request_token = set_request_context(_context("reader-error"))
    try:
        with pytest.raises(ProviderError):
            read_iac_files(["main.tf"], config=current, reader=reader)

        assert service_module._active_config.get() is previous
        assert audit_router.observed_groups == ["/audit/current"]
        events = audit_router.sink_for("/audit/current").events
        assert [event["outcome"] for event in events] == ["error"]
    finally:
        reset_request_context(request_token)
        service_module._active_config.reset(previous_token)


def test_nested_reads_restore_outer_config_for_completion_audit(audit_router):
    previous = _config("/audit/previous")
    outer_config = _config("/audit/outer")
    inner_config = _config("/audit/inner")
    inner_reader = FakeProvider().program("get_files", returns=_served("inner.tf"))

    def read_inner(**_kwargs):
        inner = read_iac_files(["inner.tf"], config=inner_config, reader=inner_reader)
        assert inner.files[0].path == "inner.tf"
        return _served("outer.tf")

    outer_reader = FakeProvider().program("get_files", returns=read_inner)
    previous_token = service_module._active_config.set(previous)
    request_token = set_request_context(_context("reader-nested"))
    try:
        outer = read_iac_files(["outer.tf"], config=outer_config, reader=outer_reader)

        assert outer.files[0].path == "outer.tf"
        assert audit_router.observed_groups == ["/audit/inner", "/audit/outer"]
        assert _event_paths(audit_router.sink_for("/audit/inner")) == [["inner.tf"]]
        assert _event_paths(audit_router.sink_for("/audit/outer")) == [["outer.tf"]]
        assert service_module._active_config.get() is previous
    finally:
        reset_request_context(request_token)
        service_module._active_config.reset(previous_token)


def test_concurrent_asyncio_task_contexts_restore_and_route_independently(audit_router):
    barrier = threading.Barrier(2)

    async def run_one(label: str) -> tuple[str, SourceControlConfig | None]:
        previous = _config(f"/audit/previous-{label}")
        current = _config(f"/audit/task-{label}")

        def overlapping_result(**_kwargs):
            barrier.wait(timeout=5)
            return _served(f"{label}.tf")

        reader = FakeProvider().program("get_files", returns=overlapping_result)
        previous_token = service_module._active_config.set(previous)
        request_token = set_request_context(_context(f"reader-task-{label}"))
        try:

            def invoke_in_worker() -> SourceControlConfig | None:
                read_iac_files([f"{label}.tf"], config=current, reader=reader)
                return service_module._active_config.get()

            restored = await asyncio.to_thread(invoke_in_worker)
            return label, restored
        finally:
            reset_request_context(request_token)
            service_module._active_config.reset(previous_token)

    async def run_both():
        return await asyncio.gather(run_one("a"), run_one("b"))

    results = dict(asyncio.run(run_both()))

    assert results["a"] is not None and results["a"].connector.audit_log_group == "/audit/previous-a"
    assert results["b"] is not None and results["b"].connector.audit_log_group == "/audit/previous-b"
    assert _event_paths(audit_router.sink_for("/audit/task-a")) == [["a.tf"]]
    assert _event_paths(audit_router.sink_for("/audit/task-b")) == [["b.tf"]]


def test_concurrent_thread_contexts_restore_and_route_independently(audit_router):
    barrier = threading.Barrier(2)
    restored: dict[str, SourceControlConfig | None] = {}
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def worker(label: str) -> None:
        previous = _config(f"/audit/previous-thread-{label}")
        current = _config(f"/audit/thread-{label}")

        def overlapping_result(**_kwargs):
            barrier.wait(timeout=5)
            return _served(f"{label}.tf")

        reader = FakeProvider().program("get_files", returns=overlapping_result)
        previous_token = service_module._active_config.set(previous)
        request_token = set_request_context(_context(f"reader-thread-{label}"))
        try:
            read_iac_files([f"{label}.tf"], config=current, reader=reader)
            with result_lock:
                restored[label] = service_module._active_config.get()
        except BaseException as exc:  # noqa: BLE001 - surface worker failures to the test thread
            with result_lock:
                errors.append(exc)
        finally:
            reset_request_context(request_token)
            service_module._active_config.reset(previous_token)

    threads = [threading.Thread(target=worker, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads), "concurrent read workers must finish"
    assert errors == []
    assert restored["a"] is not None and restored["a"].connector.audit_log_group == "/audit/previous-thread-a"
    assert restored["b"] is not None and restored["b"].connector.audit_log_group == "/audit/previous-thread-b"
    assert _event_paths(audit_router.sink_for("/audit/thread-a")) == [["a.tf"]]
    assert _event_paths(audit_router.sink_for("/audit/thread-b")) == [["b.tf"]]
