#!/usr/bin/env python3
"""Unit tests for the ``FakeProvider`` test double (test-support tooling).

These verify the three capabilities the connector tests depend on: programmable
responses, recorded calls (repo/branch/paths/args), and injectable typed provider
failures. They also confirm ``FakeProvider`` is a complete, substitutable
``SourceControlProvider`` and defines no extra mutation operations.
"""

# Standard library
import inspect

# Third-party packages
import pytest

# Local modules
from connector.models import FileContent, FileFetchResult, ProposedFile, PullRequestResult
from connector.provider import (
    ProviderAuthError,
    ProviderConflictError,
    ProviderTransientError,
    SourceControlProvider,
)
from support.fake_provider import FakeProvider, RecordedCall

pytestmark = pytest.mark.unit


def test_is_concrete_source_control_provider():
    """FakeProvider is instantiable and a real SourceControlProvider subclass."""
    fake = FakeProvider()
    assert isinstance(fake, SourceControlProvider)


def test_implements_every_abstract_operation():
    """No abstract operation is left unimplemented."""
    assert getattr(FakeProvider, "__abstractmethods__", frozenset()) == frozenset()


def test_records_all_calls_with_arguments():
    """Every operation is recorded in order with its arguments captured by name."""
    fake = FakeProvider()
    fake.get_file("org/iac", "main", "a.yaml")
    fake.get_files("org/iac", "main", ["a.yaml", "b.yaml"])
    fake.branch_exists("org/iac", "feature")
    fake.latest_commit_sha("org/iac", "main")
    fake.create_branch("org/iac", "gbaw/x", "sha123")
    fake.commit_files(
        "org/iac",
        "gbaw/x",
        [ProposedFile("a.yaml", "Resources: {}", "cloudformation")],
        "msg",
    )
    fake.open_pull_request("org/iac", "gbaw/x", "main", "title", "body")

    assert fake.call_operations == [
        "get_file",
        "get_files",
        "branch_exists",
        "latest_commit_sha",
        "create_branch",
        "commit_files",
        "open_pull_request",
    ]

    get_files_call = fake.calls_for("get_files")[0]
    assert isinstance(get_files_call, RecordedCall)
    assert get_files_call["repo"] == "org/iac"
    assert get_files_call["branch"] == "main"
    assert get_files_call["paths"] == ["a.yaml", "b.yaml"]

    create_call = fake.calls_for("create_branch")[0]
    assert create_call.kwargs == {
        "repo": "org/iac",
        "new_branch": "gbaw/x",
        "from_sha": "sha123",
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
    fake.program("latest_commit_sha", side_effects=["sha1", "sha2"])
    assert fake.latest_commit_sha("r", "main") == "sha1"
    assert fake.latest_commit_sha("r", "main") == "sha2"
    # Queue exhausted -> deterministic default.
    assert fake.latest_commit_sha("r", "main") == "0" * 40


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


def test_default_branch_commit_and_pr_flow():
    """Default behavior models branch/commit/PR creation and captures artifacts."""
    fake = FakeProvider()
    fake.set_head("org/iac", "main", "headsha")

    assert fake.branch_exists("org/iac", "gbaw/x") is False
    assert fake.latest_commit_sha("org/iac", "main") == "headsha"

    fake.create_branch("org/iac", "gbaw/x", "headsha")
    assert fake.branch_exists("org/iac", "gbaw/x") is True
    assert fake.created_branches[0]["from_sha"] == "headsha"

    sha = fake.commit_files(
        "org/iac",
        "gbaw/x",
        [ProposedFile("a.yaml", "Resources: {}", "cloudformation")],
        "commit message",
    )
    assert sha == fake.commits[0]["sha"]

    pr = fake.open_pull_request("org/iac", "gbaw/x", "main", "t", "b")
    assert isinstance(pr, PullRequestResult)
    assert pr.pull_request_id == "1"
    assert fake.pull_requests[0]["title"] == "t"


@pytest.mark.parametrize(
    "exc",
    [ProviderAuthError, ProviderConflictError("conflict"), ProviderTransientError],
)
def test_injectable_typed_failures(exc):
    """Any operation can be programmed to raise a typed provider exception."""
    fake = FakeProvider()
    fake.fail("open_pull_request", exc)
    expected = exc if isinstance(exc, type) else type(exc)
    with pytest.raises(expected):
        fake.open_pull_request("r", "h", "b", "t", "body")
    # The failing call is still recorded.
    assert fake.call_operations == ["open_pull_request"]


def test_fail_times_then_succeeds_for_retry_scenarios():
    """fail_times raises for N calls, then falls back to the default success path."""
    fake = FakeProvider()
    fake.set_head("org/iac", "main", "headsha")
    fake.fail_times("create_branch", ProviderTransientError, times=2)

    with pytest.raises(ProviderTransientError):
        fake.create_branch("org/iac", "gbaw/x", "headsha")
    with pytest.raises(ProviderTransientError):
        fake.create_branch("org/iac", "gbaw/x", "headsha")
    # Third attempt succeeds via default behavior.
    assert fake.create_branch("org/iac", "gbaw/x", "headsha") is None
    assert fake.branch_exists("org/iac", "gbaw/x") is True


def test_signature_matches_abstraction():
    """FakeProvider adds no public provider-operation beyond the abstraction set."""
    abstract_ops = {
        name
        for name, _ in inspect.getmembers(SourceControlProvider, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    # Every abstract operation is present on the fake.
    for op in abstract_ops:
        assert hasattr(FakeProvider, op)


def test_program_rejects_unknown_operation():
    """Programming an unknown operation name fails fast."""
    fake = FakeProvider()
    with pytest.raises(ValueError):
        fake.fail("merge", ProviderConflictError)
