#!/usr/bin/env python3
"""Property-based test: enablement governs Orchestrator tool exposure.

Covers Correctness Property 3 from the source-control-connector design.
``run_orchestrator`` (``agents/orchestrator.py``) builds the specialist tool set
conditionally on ``ConnectorConfig.load().enabled``:

- When ``enabled`` is ``True``, the ``source_control_agent`` write-path specialist IS
  appended to the baseline read-only set (Req 1.2).
- When ``enabled`` is ``False``, the tool set is EXACTLY the baseline read-only set
  ``[gamelift_agent, eks_agent, cost_agent]`` — the write path is not exposed, so the
  platform behaves identically to a deployment without the Connector (Req 1.3).

The property is universally quantified over the resolved config's ``enabled`` flag,
which Hypothesis drives with booleans. The Bedrock model factories and the ``Agent``
constructor are mocked so ``run_orchestrator`` executes offline and cheaply; we capture
the ``tools`` kwarg passed to ``Agent`` at that seam and assert the biconditional.

Validates: Requirements 1.2, 1.3
"""

# Standard library
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
import agents.orchestrator as orch

# The autouse ``mock_all_mcp_and_agents`` conftest fixture patches
# ``agents.orchestrator.run_orchestrator`` for unit tests. Capture the genuine
# implementation at import time so the real tool-list-building path executes.
_real_run_orchestrator = orch.run_orchestrator

pytestmark = pytest.mark.unit


def _capture_orchestrator_tools(enabled: bool) -> list:
    """Run the REAL ``run_orchestrator`` and return the ``tools`` kwarg given to ``Agent``.

    ``ConnectorConfig.load()`` is stubbed to a config with the requested ``enabled``
    flag; the Bedrock model factories and the ``Agent`` constructor are mocked so the
    invocation is cheap and offline. Passing ``context=None`` keeps the run on the
    no-memory fallback path, where ``Agent(...)`` is constructed with the assembled tool
    set. Uses ``mock.patch`` context managers (not the function-scoped ``monkeypatch``
    fixture) so each Hypothesis-generated example is exercised in isolation.
    """
    captured = {}

    class _CapturingAgent:
        def __init__(self, *args, **kwargs):
            captured["tools"] = kwargs.get("tools")

        def __call__(self, query):  # Agent(query) -> response
            return "ok"

    fake_config = SimpleNamespace(enabled=enabled)

    with (
        mock.patch.object(orch.ConnectorConfig, "load", lambda: fake_config),
        mock.patch.object(orch, "create_cached_bedrock_model", lambda *a, **k: MagicMock()),
        mock.patch.object(orch, "create_bedrock_model_with_overrides", lambda *a, **k: MagicMock()),
        mock.patch.object(orch, "Agent", _CapturingAgent),
    ):
        _real_run_orchestrator("propose a change to the vpc template", context=None)

    assert "tools" in captured, "Agent was never constructed"
    return captured["tools"]


# Feature: source-control-connector, Property 3: Enablement governs tool exposure
@settings(max_examples=100)
@given(enabled=st.booleans())
def test_property3_enablement_governs_tool_exposure(enabled):
    """The source_control_agent is in the tool set iff config.enabled is True.

    - Disabled: the tool set is EXACTLY the baseline read-only specialists, in order,
      and the write-path specialist is absent (Req 1.3).
    - Enabled: the baseline set is preserved and the source_control_agent is additively
      included (Req 1.2).
    """
    # Local modules
    import agents.source_control_specialist as scs

    baseline = [orch.gamelift_agent, orch.eks_agent, orch.cost_agent]

    tools = _capture_orchestrator_tools(enabled)

    if enabled:
        # Baseline set unchanged and still present ...
        for baseline_agent in baseline:
            assert baseline_agent in tools
        # ... and the write-path specialist is additively included.
        assert scs.source_control_agent in tools
        assert len(tools) == len(baseline) + 1
    else:
        # Exactly the baseline read-only set, in order, and nothing else.
        assert tools == baseline
        assert scs.source_control_agent not in tools
