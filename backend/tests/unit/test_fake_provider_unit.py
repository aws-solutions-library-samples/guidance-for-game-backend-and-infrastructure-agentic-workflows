#!/usr/bin/env python3
"""Unit tests for the ``FakeProvider`` read-only test double (test-support tooling).

These verify the three capabilities the connector read tests depend on: programmable
responses, recorded calls (repo/branch/paths/args), and injectable typed provider
failures. They also confirm ``FakeProvider`` is a complete, substitutable
``SourceControlReader`` and defines no mutation operation.
"""

# Standard library
import inspect

# Third-party packages
import pytest

# Local modules
from connector.models import FileContent, FileFetchResult
from connector.provider import (
    ProviderAuthError,
    ProviderTransientError,
    ProviderUnavailableError,
    SourceControlReader,
)
from support.fake_provider import FakeProvider, RecordedCall

pytestmark = pytest.mark.unit


def test_is_concrete_source_control_reader():
    """FakeProvider is instantiable and a real SourceControlReader subclass."""
    fake = FakeProvider()
    assert isinstance(fake, SourceControlReader)


def test_implements_every_abstract_operation():
    """No abstract operation is left unimplemented."""
    assert getattr(FakeProvider, "__abstractmethods__", frozenset()) == frozenset()


def test_records_all_calls_with_arguments():
    """Every read operation is recorded in order with its arguments captured by name."""
    fake = FakeProvider()
    fake.get_file("org/iac", "main", "a.yaml")
    fake.get_files("org/iac", "main", ["a.yaml", "b.yaml"])

    assert fake.call_operations == ["get_file", "get_files"]

    get_files_call = fake.calls_for("get_files")[0]
    assert isinstance(get_files_call, RecordedCall)
    assert get_files_call["repo"] == "org/iac"
    assert get_files_call["branch"] == "main"
    assert get_files_call["paths"] == ["a.yaml", "b.yaml"]

    get_file_call = fake.calls_for("get_file")[0]
    assert get_file_call.kwargs == {
        "repo": "org/iac",
        "branch": "main",
        "path": "a.yaml",
    }


def test_programmable_fixed_return_value():
    """A pinned return value is used on every call."""
    fake = FakeProvider()
    pinned = FileFetchResult(files=(FileContent("a", "b"),), missing=("c",), limit_exceeded=True)
    fake.set_return("get_files", pinned)
    assert fake.get_files("r", "main", ["a"]) is pinned
    assert fake.get_files("r", "main", ["x", "y"]) is pinned


def test_programmable_sequence_of_side_effects():
    """Queued side effects are consumed one per call, then fall back to default."""
    fake = FakeProvider()
    a = FileContent("a.yaml", "one")
    b = FileContent("a.yaml", "two")
    fake.program("get_file", side_effects=[a, b])
    assert fake.get_file("r", "main", "a.yaml") is a
    assert fake.get_file("r", "main", "a.yaml") is b
    # Queue exhausted -> deterministic default (file not seeded -> None).
    assert fake.get_file("r", "main", "a.yaml") is None


def test_default_stateful_read_and_missing_reporting():
    """With seeded files, reads resolve; unseeded paths report as missing."""
    fake = FakeProvider()
    fake.add_file("org/iac", "main", "a.yaml", "Resources: {}")

    got = fake.get_file("org/iac", "main", "a.yaml")
    assert got == FileContent(path="a.yaml", content="Resources: {}")
    assert fake.get_file("org/iac", "main", "missing.yaml") is None

    result = fake.get_files("org/iac", "main", ["a.yaml", "missing.yaml"])
    assert result.files == (FileContent("a.yaml", "Resources: {}"),)
    assert result.missing == ("missing.yaml",)
    assert result.limit_exceeded is False


@pytest.mark.parametrize(
    "exc",
    [ProviderAuthError, ProviderUnavailableError("down"), ProviderTransientError],
)
def test_injectable_typed_failures(exc):
    """Any read operation can be programmed to raise a typed provider exception."""
    fake = FakeProvider()
    fake.fail("get_files", exc)
    expected = exc if isinstance(exc, type) else type(exc)
    with pytest.raises(expected):
        fake.get_files("r", "main", ["a.yaml"])
    # The failing call is still recorded.
    assert fake.call_operations == ["get_files"]


def test_fail_times_then_succeeds():
    """fail_times raises for N calls, then falls back to the default read path."""
    fake = FakeProvider()
    fake.add_file("org/iac", "main", "a.yaml", "content")
    fake.fail_times("get_file", ProviderTransientError, times=2)

    with pytest.raises(ProviderTransientError):
        fake.get_file("org/iac", "main", "a.yaml")
    with pytest.raises(ProviderTransientError):
        fake.get_file("org/iac", "main", "a.yaml")
    # Third attempt succeeds via default behavior.
    assert fake.get_file("org/iac", "main", "a.yaml") == FileContent("a.yaml", "content")


def test_signature_matches_abstraction():
    """FakeProvider adds no public provider-operation beyond the read abstraction set."""
    abstract_ops = {
        name
        for name, _ in inspect.getmembers(SourceControlReader, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    # Every abstract operation is present on the fake.
    for op in abstract_ops:
        assert hasattr(FakeProvider, op)
    # The read abstraction is exactly get_file / get_files.
    assert abstract_ops == {"get_file", "get_files"}


def test_program_rejects_unknown_operation():
    """Programming an unknown operation name fails fast."""
    fake = FakeProvider()
    with pytest.raises(ValueError):
        fake.fail("create_branch", ProviderTransientError)
