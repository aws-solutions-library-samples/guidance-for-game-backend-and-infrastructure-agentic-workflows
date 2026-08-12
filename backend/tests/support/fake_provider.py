"""In-memory ``SourceControlReader`` test double (read-only).

``FakeProvider`` implements every operation of the read-only
:class:`connector.provider.SourceControlReader` abstraction so unit and property-based
tests can exercise the connector read service without any network or AWS calls (see the
design's Testing Strategy: "a ``FakeReader`` implementing ``SourceControlReader`` with
programmable responses/failures"). It offers three capabilities tests rely on:

1. **Programmable responses** — per read operation, callers can pin a fixed return value,
   or queue an ordered sequence of outcomes (values and/or exceptions) that are consumed
   one per call. This drives exact-value assertions and failure scenarios.
2. **Recorded calls** — every invocation is appended to :attr:`calls` as a
   :class:`RecordedCall` capturing the operation name and all arguments (repo, branch,
   paths), so tests can assert *which* provider operations ran, in what order, and that no
   provider read ran on a rejected path.
3. **Injectable typed failures** — callers can make any read operation raise one of the
   abstraction's typed exceptions (``ProviderUnavailableError``, ``ProviderAuthError``,
   ``ProviderTransientError``) either on every call or for a bounded number of calls.

When no response is programmed for an operation, the fake falls back to a small,
deterministic in-memory model of a repository (a file store) so it behaves like a plausible
read provider. The provider-write path has been removed from the connector; this double is
read-only and defines no mutation operation.
"""

from __future__ import annotations

# Standard library
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Local modules
from connector.models import FileContent, FileFetchResult
from connector.provider import SourceControlReader

# Sentinel indicating "nothing programmed for this operation".
_UNSET: Any = object()

# The fixed read operation set of the abstraction (used to validate programming keys and
# to reset per-operation state).
OPERATIONS: tuple[str, ...] = (
    "get_file",
    "get_files",
)


@dataclass
class RecordedCall:
    """A single recorded invocation of a read operation.

    ``operation`` is the method name; ``kwargs`` holds every argument by name so tests
    can assert on repo/branch/paths regardless of positional vs keyword calling.
    """

    operation: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:  # convenience for terse assertions
        return self.kwargs[key]


class FakeProvider(SourceControlReader):
    """Programmable, call-recording in-memory ``SourceControlReader`` (read-only).

    Example
    -------
    >>> fake = FakeProvider()
    >>> fake.add_file("org/iac", "main", "a.yaml", "Resources: {}")
    >>> fake.get_file("org/iac", "main", "a.yaml").content
    'Resources: {}'
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

    # ------------------------------------------------------------------ programming

    def program(
        self,
        operation: str,
        *,
        returns: Any = _UNSET,
        raises: BaseException | type[BaseException] | None = None,
        side_effects: list[Any] | None = None,
    ) -> FakeProvider:
        """Program the outcome(s) of a read ``operation``.

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
        return self

    # ---------------------------------------------------------- outcome resolution

    def _check_operation(self, operation: str) -> None:
        if operation not in OPERATIONS:
            raise ValueError(f"Unknown read operation {operation!r}; valid operations are {OPERATIONS}")

    def _record(self, operation: str, **kwargs: Any) -> None:
        self.calls.append(RecordedCall(operation=operation, kwargs=kwargs))

    def _programmed_outcome(self, operation: str) -> Any:
        """Return the next programmed outcome for ``operation`` or ``_UNSET``."""
        queue = self._queues[operation]
        if queue:
            return queue.popleft()
        return self._persistent[operation]

    @staticmethod
    def _is_exception(outcome: Any) -> bool:
        return isinstance(outcome, BaseException) or (isinstance(outcome, type) and issubclass(outcome, BaseException))

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

    # --------------------------------------------------------------- introspection

    def calls_for(self, operation: str) -> list[RecordedCall]:
        """Return the recorded calls for a single ``operation`` in order."""
        self._check_operation(operation)
        return [c for c in self.calls if c.operation == operation]

    @property
    def call_operations(self) -> list[str]:
        """The ordered list of operation names invoked so far."""
        return [c.operation for c in self.calls]
