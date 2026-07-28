#!/usr/bin/env python3
"""Example tests: disabled-baseline tool exposure and IaC Knowledge Base wiring.

Two independent connector wiring behaviors are asserted here (example / non-property
tests):

1. **Disabled baseline (Req 1.3).** ``run_orchestrator`` builds the specialist tool set
   conditionally on ``ConnectorConfig.load().enabled``. When the Connector is disabled,
   the Orchestrator is constructed with *exactly* the baseline read-only specialist set
   ``[gamelift_agent, eks_agent, cost_agent]`` — the ``source_control_agent`` write path
   is NOT exposed, so the platform behaves exactly as it does today. When enabled, the
   ``source_control_agent`` IS appended (positive control). We capture the ``tools`` kwarg
   passed to the ``Agent`` constructor at the smallest seam and assert membership.

2. **IaC KB wiring (Req 3.5).** The ``source_control_agent`` is built through the shared
   ``create_specialist_agent`` factory with ``kb_id=SCM_IAC_KB_ID``. When an IaC KB id is
   configured (truthy), the specialist is built with the KB ``retrieve`` tool (the factory
   invokes ``create_kb_retrieve_tool`` and appends the result to the agent's tools); when
   unset, no KB tool is wired. We exercise the factory seam directly and also confirm the
   real ``source_control_specialist`` threads the configured KB id through to the factory.

These are structural/wiring guarantees, so the Bedrock model, MCP, memory, and the KB
retrieve tool are mocked — no real network or model calls occur.

Validates: Requirements 1.3, 3.5
"""

# Standard library
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

# Third-party packages
import pytest

# Local modules
import agents.orchestrator as orch

# The autouse ``mock_all_mcp_and_agents`` conftest fixture patches
# ``agents.orchestrator.run_orchestrator`` for unit tests. Capture the genuine
# implementation at import time so the tool-list-building path actually executes.
_real_run_orchestrator = orch.run_orchestrator

pytestmark = pytest.mark.unit


def _underlying_function(tool):
    """Recover the original callable wrapped by the strands ``@tool`` decorator."""
    fn = getattr(tool, "__wrapped__", None) or getattr(tool, "_tool_func", None)
    assert fn is not None, f"could not recover underlying function for tool {tool!r}"
    assert callable(fn), f"recovered underlying object for {tool!r} is not callable"
    return fn


def _run_orchestrator_capturing_tools(monkeypatch, enabled: bool):
    """Run the REAL ``run_orchestrator`` and return the ``tools`` kwarg given to ``Agent``.

    ``ConnectorConfig.load()`` is stubbed to a config with the requested ``enabled`` flag;
    the Bedrock model factories and the ``Agent`` constructor are mocked so invocation is
    cheap and offline. Passing ``context=None`` keeps the run on the no-memory fallback
    path, which is where ``Agent(...)`` is constructed with the assembled tool set.
    """
    captured = {}

    class _CapturingAgent:
        def __init__(self, *args, **kwargs):
            captured["tools"] = kwargs.get("tools")
            captured["system_prompt"] = kwargs.get("system_prompt")

        def __call__(self, query):  # Agent(query) -> response
            return "ok"

    fake_config = SimpleNamespace(enabled=enabled)

    # ConnectorConfig.load() is a classmethod; replacing it with a zero-arg callable is
    # sufficient because run_orchestrator calls it as ``ConnectorConfig.load()``.
    monkeypatch.setattr(orch.ConnectorConfig, "load", lambda: fake_config)
    monkeypatch.setattr(orch, "create_cached_bedrock_model", lambda *a, **k: MagicMock())
    monkeypatch.setattr(orch, "create_bedrock_model_with_overrides", lambda *a, **k: MagicMock())
    monkeypatch.setattr(orch, "Agent", _CapturingAgent)

    _real_run_orchestrator("propose a change to the vpc template", context=None)

    assert "tools" in captured, "Agent was never constructed"
    return captured["tools"]


# ---------------------------------------------------------------------------
# Behavior 1: enablement governs which specialists the Orchestrator exposes.
# ---------------------------------------------------------------------------


def test_disabled_config_yields_baseline_tool_list_only(monkeypatch):
    """Disabled Connector => Orchestrator gets exactly the baseline read-only set (Req 1.3)."""
    tools = _run_orchestrator_capturing_tools(monkeypatch, enabled=False)

    # Exactly the three baseline read-only specialists, in order, and nothing else.
    assert tools == [orch.gamelift_agent, orch.eks_agent, orch.cost_agent]

    # The write-path specialist must not be present in any form.
    # Local modules
    import agents.source_control_specialist as scs

    assert scs.source_control_agent not in tools
    tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools]
    assert not any("source" in str(name).lower() for name in tool_names)


def test_enabled_config_includes_source_control_specialist(monkeypatch):
    """Positive control: enabled Connector appends the source_control_agent (Req 1.2/1.3)."""
    tools = _run_orchestrator_capturing_tools(monkeypatch, enabled=True)

    # Local modules
    import agents.source_control_specialist as scs

    # Baseline set is unchanged and still present ...
    for baseline in (orch.gamelift_agent, orch.eks_agent, orch.cost_agent):
        assert baseline in tools
    # ... and the write-path specialist is additively included.
    assert scs.source_control_agent in tools
    assert len(tools) == 4


# ---------------------------------------------------------------------------
# Behavior 2: a configured IaC KB wires the retrieve tool into the specialist.
# ---------------------------------------------------------------------------


def _build_and_invoke_specialist(monkeypatch, kb_id):
    """Build a specialist via the real factory and invoke it, capturing wiring effects.

    Returns ``(tools_passed_to_Agent, kb_factory_mock)`` so callers can assert whether the
    KB ``retrieve`` tool was created and included based on ``kb_id`` truthiness.
    """
    # Local modules
    import agents.base_specialist as base

    captured = {}

    class _CapturingAgent:
        def __init__(self, *args, **kwargs):
            captured["tools"] = list(kwargs.get("tools") or [])

        def __call__(self, query):
            return "specialist-ok"

    kb_retrieve_sentinel = object()
    kb_factory = MagicMock(return_value=kb_retrieve_sentinel)

    monkeypatch.setattr(base, "Agent", _CapturingAgent)
    monkeypatch.setattr(base, "create_kb_retrieve_tool", kb_factory)
    monkeypatch.setattr(base, "create_cached_bedrock_model", lambda *a, **k: MagicMock())
    monkeypatch.setattr(base, "create_bedrock_model_with_overrides", lambda *a, **k: MagicMock())

    extra_tool = MagicMock(name="additional_tool")

    specialist = base.create_specialist_agent(
        service_name="SourceControl",
        emoji="🔀",
        mcp_server_names=None,
        kb_id=kb_id,
        prompt_fn=lambda: "system prompt",
        fallback_fn=None,
        additional_tools=[extra_tool],
    )

    _underlying_function(specialist)("read the template")

    return captured["tools"], kb_factory, kb_retrieve_sentinel, extra_tool


def test_configured_iac_kb_wires_retrieve_tool_into_specialist(monkeypatch):
    """Truthy kb_id => specialist is built WITH the KB retrieve tool (Req 3.5)."""
    tools, kb_factory, kb_retrieve_sentinel, extra_tool = _build_and_invoke_specialist(
        monkeypatch, kb_id="iac-kb-123"
    )

    # The factory created the retrieve tool for the configured KB id ...
    kb_factory.assert_called_once()
    assert kb_factory.call_args.args[0] == "iac-kb-123"
    # ... and it was included alongside the connector's own tools.
    assert kb_retrieve_sentinel in tools
    assert extra_tool in tools


def test_unset_iac_kb_builds_specialist_without_retrieve_tool(monkeypatch):
    """Falsy kb_id => no KB retrieve tool is created or wired (Req 3.5)."""
    tools, kb_factory, kb_retrieve_sentinel, extra_tool = _build_and_invoke_specialist(
        monkeypatch, kb_id=None
    )

    kb_factory.assert_not_called()
    assert kb_retrieve_sentinel not in tools
    # Only the connector's own tools remain.
    assert tools == [extra_tool]


def test_real_source_control_specialist_threads_configured_kb_id_to_factory():
    """The real specialist passes SCM_IAC_KB_ID through to create_specialist_agent (Req 3.5).

    Combined with the factory tests above (truthy kb_id => KB retrieve tool wired), this
    proves that configuring an IaC KB causes the source_control specialist to be built with
    the KB retrieve tool. Uses manual patch + reload with a guaranteed restore so no reloaded
    module state leaks to other tests.
    """
    # Local modules
    import agents.base_specialist as base
    import agents.source_control_specialist as scs
    import config.settings as settings

    original_kb = settings.SCM_IAC_KB_ID
    original_factory = base.create_specialist_agent

    captured = {}

    def _fake_factory(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="source_control_agent")

    try:
        settings.SCM_IAC_KB_ID = "iac-kb-42"
        base.create_specialist_agent = _fake_factory
        importlib.reload(scs)

        assert captured.get("kb_id") == "iac-kb-42"
        # The write-path tools are still wired regardless of KB configuration.
        additional = captured.get("additional_tools") or []
        names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in additional}
        assert "get_iac_file" in names
        assert "propose_infrastructure_change" in names
    finally:
        # Restore settings and the real factory, then rebuild the real specialist object so
        # subsequent tests (and the orchestrator's local import) see the genuine agent.
        settings.SCM_IAC_KB_ID = original_kb
        base.create_specialist_agent = original_factory
        importlib.reload(scs)
