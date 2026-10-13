#!/usr/bin/env python3
"""Shared test helpers for isolating the telemetry content guard's process state.

Importing ``agentcore_main`` runs ``apply_trace_content_guard`` against the real
process environment. The guard is process-wide: it writes four OTEL/GBAW
environment variables, installs wrappers on the Strands ``Tracer`` class methods,
and attaches a logging filter to the Strands MCP client logger. Tests that import
the entrypoint therefore mutate shared state that would otherwise persist for the
rest of the pytest session.

``snapshot_guard_state`` / ``restore_guard_state`` capture and restore exactly
that state, and ``guard_filter_count`` counts the guard's own logging filter so a
test can assert whether the guard installed one. ``telemetry_guard_isolation`` is
an autouse fixture body that snapshots before a test, restores afterwards, and
pops ``agentcore_main`` from ``sys.modules`` so the next importer re-runs the
guard from a known starting point.
"""

# Standard library
import logging
import os
import sys
from typing import Any, Dict

# The environment variables the guard writes. Snapshotting these lets a test
# restore the environment to its pre-import value.
_GUARD_ENV_VARS = (
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    "OTEL_SEMCONV_STABILITY_OPT_IN",
    "GBAW_CAPTURE_TRACE_CONTENT",
    "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS",
)


def guard_filter_count() -> int:
    """Return how many of the guard's own logging filters are installed.

    Counts only filters carrying the guard's sentinel marker on the Strands MCP
    client logger, so unrelated filters another test may have added do not affect
    the result.
    """
    # Local modules
    from config.telemetry_guard import MCP_EXCEPTION_LOGGER_NAMES, _McpExceptionInfoFilter

    total = 0
    for name in MCP_EXCEPTION_LOGGER_NAMES:
        total += sum(
            1
            for flt in logging.getLogger(name).filters
            if getattr(flt, "marker", None) == _McpExceptionInfoFilter.marker
        )
    return total


def snapshot_guard_state() -> Dict[str, Any]:
    """Capture the process state the content guard mutates on import."""
    # Third-party packages
    from strands.telemetry.tracer import Tracer

    # Local modules
    from config.telemetry_guard import MCP_EXCEPTION_LOGGER_NAMES

    return {
        "env": {var: os.environ.get(var) for var in _GUARD_ENV_VARS},
        "start_agent_span": Tracer.start_agent_span,
        "end_span": Tracer._end_span,
        "filters": {name: list(logging.getLogger(name).filters) for name in MCP_EXCEPTION_LOGGER_NAMES},
    }


def restore_guard_state(state: Dict[str, Any]) -> None:
    """Restore the state captured by :func:`snapshot_guard_state`."""
    # Third-party packages
    from strands.telemetry.tracer import Tracer

    for var, value in state["env"].items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    Tracer.start_agent_span = state["start_agent_span"]
    Tracer._end_span = state["end_span"]
    for name, flts in state["filters"].items():
        lg = logging.getLogger(name)
        for flt in list(lg.filters):
            if flt not in flts:
                lg.removeFilter(flt)


def telemetry_guard_isolation():
    """Autouse-fixture body: isolate the guard's process state around a test.

    Snapshots the guard state, yields, then restores it and drops any cached
    ``agentcore_main`` module so the next importer runs the guard from the same
    starting point this test saw. Written as a plain generator so each test module
    can wrap it in its own ``@pytest.fixture(autouse=True)``.
    """
    state = snapshot_guard_state()
    try:
        yield
    finally:
        restore_guard_state(state)
        sys.modules.pop("agentcore_main", None)
