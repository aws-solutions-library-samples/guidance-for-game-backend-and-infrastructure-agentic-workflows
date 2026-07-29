"""In-memory ``SourceControlProvider`` test double.

``FakeProvider`` implements every operation of the
:class:`connector.provider.SourceControlProvider` abstraction so unit and
property-based tests can exercise the connector service layer without any network
or AWS calls (see the design's Testing Strategy: "a ``FakeProvider`` implementing
``SourceControlProvider`` with programmable responses/failures"). It offers three
capabilities tests rely on:

1. **Programmable responses** — per operation, callers can pin a fixed return value,
   or queue an ordered sequence of outcomes (values and/or exceptions) that are
   consumed one per call. This drives retry scenarios (e.g. "two transient failures
   then success") and exact-value assertions.
2. **Recorded calls** — every invocation is appended to :attr:`calls` as a
   :class:`RecordedCall` capturing the operation name and all arguments (repo, branch,
   paths, files, message, etc.), so tests can assert *which* provider operations ran,
   in what order, and that no operation ran on a rejected/declined path.
3. **Injectable typed failures** — callers can make any operation raise one of the
   abstraction's typed exceptions (``ProviderUnavailableError``, ``ProviderAuthError``,
   ``ProviderConflictError``, ``ProviderTransientError``) either on every call or for a
   bounded number of calls.

When no response is programmed for an operation, the fake falls back to a small,
deterministic in-memory model of a repository (a file store, a set of existing
branches, and per-branch head SHAs) so it behaves like a plausible provider for
read/branch/commit/PR flows.
"""

# Standard library
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Local modules
from connector.models import (
    FileContent,
    FileFetchResult,
    ProposedFile,
    ChangeProposalResult,
)
from connector.provider import SourceControlProvider

if TYPE_CHECKING:
    # Standard library
    from collections.abc import Callable


# Sentinel indicating "nothing programmed for this operation".
_UNSET: Any = object()

# The fixed operation set of the abstraction (used to validate programming keys and
# to reset per-operation state).
OPERATIONS: tuple[str, ...] = (
    "get_file",
    "get_files",
    "branch_exists",
    "latest_commit_sha",
    "create_branch",
    "commit_files",
    "open_change_proposal",
    "find_open_change_proposal",
)


@dataclass
class RecordedCall:
    """A single recorded invocation of a provider operation.

    ``operation`` is the method name; ``kwargs`` holds every argument by name so tests
    can assert on repo/branch/paths/args regardless of positional vs keyword calling.
    """

    operation: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:  # convenience for terse assertions
        return self.kwargs[key]


class FakeProvider(SourceControlProvider):
    """Programmable, call-recording in-memory ``SourceControlProvider``.

    Example
    -------
    >>> fake = FakeProvider()
    >>> fake.add_file("org/iac", "main", "a.yaml", "Resources: {}")
    >>> fake.get_file("org/iac", "main", "a.yaml").content
    'Resources: {}'
    >>> fake.fail("open_change_proposal", ProviderConflictError("boom"))
    >>> [c.operation for c in fake.calls]
    ['get_file']
    """

    def __init__(self) -> None:
        # Recorded calls, in invocation order.
        self.calls: list[RecordedCall] = []

        # Programmed outcome queues (values and/or exceptions), consumed one per call.
        self._queues: dict[str, deque[Any]] = {op: deque() for op in OPERATIONS}
        # Persistent programmed outcome, used when the queue is empty (a value or an
        # exception applied to every subsequent call).
        self._persistent: dict[str, Any] = {op: _UNSET for op in OPERATIONS}

        # Deterministic in-memory repository model used as the default behavior.
        self._files: dict[tuple[str, str, str], str] = {}
        self._branches: set[tuple[str, str]] = set()
        self._head_shas: dict[tuple[str, str], str] = {}

        # Capture structures for created artifacts (in addition to ``calls``).
        self.created_branches: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []
        self.pull_requests: list[dict[str, Any]] = []

        # Monotonic counters for deterministic generated identifiers.
        self._pr_counter = itertools.count(1)
        self._commit_counter = itertools.count(1)

    # ------------------------------------------------------------------ programming

    def program(
        self,
        operation: str,
        *,
        returns: Any = _UNSET,
        raises: BaseException | type[BaseException] | None = None,
        side_effects: list[Any] | None = None,
    ) -> FakeProvider:
        """Program the outcome(s) of ``operation``.

        - ``returns``: a fixed value returned on every call (until a queued side effect
          takes precedence). If callable, it is invoked with the call kwargs.
        - ``raises``: an exception (instance or class) raised on every call.
        - ``side_effects``: an ordered list consumed one entry per call; each entry may
          be an exception (raised) or any other value (returned). When exhausted, the
          fake falls back to ``returns``/``raises``/default behavior.

        Returns ``self`` to allow fluent chaining.
        """
        self._check_operation(operation)
        if side_effects is not None:
            self._queues[operation].extend(side_effects)
        if raises is not None:
            self._persistent[operation] = raises
        elif returns is not _UNSET:
            self._persistent[operation] = returns
        return self

    def set_return(self, operation: str, value: Any) -> FakeProvider:
        """Pin ``operation`` to return ``value`` on every call."""
        return self.program(operation, returns=value)

    def fail(
        self,
        operation: str,
        exc: BaseException | type[BaseException],
    ) -> FakeProvider:
        """Make ``operation`` raise ``exc`` on every call (injected typed failure)."""
        return self.program(operation, raises=exc)

    def fail_times(
        self,
        operation: str,
        exc: BaseException | type[BaseException],
        times: int,
    ) -> FakeProvider:
        """Make ``operation`` raise ``exc`` for the next ``times`` calls, then fall
        back to any persistent programming or the default behavior.

        Useful for transient-error retry scenarios (raise N times, then succeed).
        """
        self._check_operation(operation)
        self._queues[operation].extend([exc] * max(0, times))
        return self

    def reset_calls(self) -> None:
        """Clear the recorded call log (leaves programming and state intact)."""
        self.calls.clear()

    # ----------------------------------------------------------- state convenience

    def add_file(self, repo: str, branch: str, path: str, content: str) -> FakeProvider:
        """Seed the in-memory file store so reads find ``path`` on ``branch``."""
        self._files[(repo, branch, path)] = content
        self._branches.add((repo, branch))
        return self

    def add_branch(self, repo: str, branch: str, head_sha: str = "") -> FakeProvider:
        """Mark ``branch`` as already existing in ``repo`` (optionally with a head SHA)."""
        self._branches.add((repo, branch))
        if head_sha:
            self._head_shas[(repo, branch)] = head_sha
        return self

    def set_head(self, repo: str, branch: str, sha: str) -> FakeProvider:
        """Set the latest commit SHA reported for ``branch``."""
        self._head_shas[(repo, branch)] = sha
        self._branches.add((repo, branch))
        return self

    # ---------------------------------------------------------- outcome resolution

    def _check_operation(self, operation: str) -> None:
        if operation not in OPERATIONS:
            raise ValueError(
                f"Unknown provider operation {operation!r}; "
                f"valid operations are {OPERATIONS}"
            )

    def _record(self, operation: str, **kwargs: Any) -> None:
        self.calls.append(RecordedCall(operation=operation, kwargs=kwargs))

    def _programmed_outcome(self, operation: str) -> Any:
        """Return the next programmed outcome for ``operation`` or ``_UNSET``.

        The queued ``side_effects`` are consumed first; when empty the persistent
        programmed value/exception (if any) is used.
        """
        queue = self._queues[operation]
        if queue:
            return queue.popleft()
        return self._persistent[operation]

    @staticmethod
    def _is_exception(outcome: Any) -> bool:
        return isinstance(outcome, BaseException) or (
            isinstance(outcome, type) and issubclass(outcome, BaseException)
        )

    def _apply(self, outcome: Any, **kwargs: Any) -> Any:
        """Turn a programmed outcome into a value (raising it if it is an exception)."""
        if self._is_exception(outcome):
            raise outcome if isinstance(outcome, BaseException) else outcome()
        if callable(outcome):
            return outcome(**kwargs)
        return outcome

    # --------------------------------------------------------- provider operations

    def get_file(self, repo: str, branch: str, path: str) -> FileContent | None:
        self._record("get_file", repo=repo, branch=branch, path=path)
        outcome = self._programmed_outcome("get_file")
        if outcome is not _UNSET:
            return self._apply(outcome, repo=repo, branch=branch, path=path)
        content = self._files.get((repo, branch, path))
        return FileContent(path=path, content=content) if content is not None else None

    def get_files(self, repo: str, branch: str, paths: list[str]) -> FileFetchResult:
        self._record("get_files", repo=repo, branch=branch, paths=list(paths))
        outcome = self._programmed_outcome("get_files")
        if outcome is not _UNSET:
            return self._apply(outcome, repo=repo, branch=branch, paths=list(paths))
        found: list[FileContent] = []
        missing: list[str] = []
        for path in paths:
            content = self._files.get((repo, branch, path))
            if content is not None:
                found.append(FileContent(path=path, content=content))
            else:
                missing.append(path)
        return FileFetchResult(
            files=tuple(found),
            missing=tuple(missing),
            limit_exceeded=False,
        )

    def branch_exists(self, repo: str, branch: str) -> bool:
        self._record("branch_exists", repo=repo, branch=branch)
        outcome = self._programmed_outcome("branch_exists")
        if outcome is not _UNSET:
            return self._apply(outcome, repo=repo, branch=branch)
        return (repo, branch) in self._branches

    def latest_commit_sha(self, repo: str, branch: str) -> str:
        self._record("latest_commit_sha", repo=repo, branch=branch)
        outcome = self._programmed_outcome("latest_commit_sha")
        if outcome is not _UNSET:
            return self._apply(outcome, repo=repo, branch=branch)
        return self._head_shas.get((repo, branch), "0" * 40)

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        self._record(
            "create_branch", repo=repo, new_branch=new_branch, from_sha=from_sha
        )
        outcome = self._programmed_outcome("create_branch")
        if outcome is not _UNSET:
            self._apply(outcome, repo=repo, new_branch=new_branch, from_sha=from_sha)
            return None
        self.created_branches.append(
            {"repo": repo, "new_branch": new_branch, "from_sha": from_sha}
        )
        self._branches.add((repo, new_branch))
        self._head_shas[(repo, new_branch)] = from_sha
        return None

    def commit_files(
        self,
        repo: str,
        branch: str,
        files: list[ProposedFile],
        message: str,
    ) -> str:
        self._record(
            "commit_files",
            repo=repo,
            branch=branch,
            files=list(files),
            message=message,
        )
        outcome = self._programmed_outcome("commit_files")
        if outcome is not _UNSET:
            return self._apply(
                outcome, repo=repo, branch=branch, files=list(files), message=message
            )
        sha = f"commit{next(self._commit_counter)}"
        self.commits.append(
            {
                "repo": repo,
                "branch": branch,
                "files": list(files),
                "message": message,
                "sha": sha,
            }
        )
        for proposed in files:
            self._files[(repo, branch, proposed.path)] = proposed.content
        self._head_shas[(repo, branch)] = sha
        return sha

    def open_change_proposal(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> ChangeProposalResult:
        self._record(
            "open_change_proposal",
            repo=repo,
            head=head,
            base=base,
            title=title,
            body=body,
        )
        outcome = self._programmed_outcome("open_change_proposal")
        if outcome is not _UNSET:
            return self._apply(
                outcome, repo=repo, head=head, base=base, title=title, body=body
            )
        pr_number = next(self._pr_counter)
        pr_id = str(pr_number)
        pr_url = f"https://fake.provider/{repo}/pull/{pr_number}"
        self.pull_requests.append(
            {
                "repo": repo,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "proposal_id": pr_id,
                "proposal_url": pr_url,
            }
        )
        return ChangeProposalResult(proposal_id=pr_id, proposal_url=pr_url)

    def find_open_change_proposal(
        self,
        repo: str,
        head: str,
        base: str,
    ) -> ChangeProposalResult | None:
        """Reconciliation query used by reconcile-before-retry (Req 12.4).

        Records the call and honors programmed outcomes exactly like the other operations
        (a pinned ``returns`` value, a queued ``side_effects`` sequence, or an injected
        typed failure). When nothing is programmed it falls back to the deterministic
        in-memory model: it returns the most recent recorded open proposal whose
        ``head``/``base`` match the query, or ``None`` when none has been opened — mirroring
        the base ABC default of "none found".
        """
        self._record(
            "find_open_change_proposal", repo=repo, head=head, base=base
        )
        outcome = self._programmed_outcome("find_open_change_proposal")
        if outcome is not _UNSET:
            return self._apply(outcome, repo=repo, head=head, base=base)
        for pr in reversed(self.pull_requests):
            if pr["repo"] == repo and pr["head"] == head and pr["base"] == base:
                return ChangeProposalResult(
                    proposal_id=pr["proposal_id"], proposal_url=pr["proposal_url"]
                )
        return None

    # --------------------------------------------------------------- introspection

    def calls_for(self, operation: str) -> list[RecordedCall]:
        """Return the recorded calls for a single ``operation`` in order."""
        self._check_operation(operation)
        return [c for c in self.calls if c.operation == operation]

    @property
    def call_operations(self) -> list[str]:
        """The ordered list of operation names invoked so far."""
        return [c.operation for c in self.calls]
