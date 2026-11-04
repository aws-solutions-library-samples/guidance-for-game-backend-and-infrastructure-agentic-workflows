"""Unit tests pinning the AgentCore memory integration import.

The orchestrator imports the AgentCore memory session manager under
``except ImportError`` and silently drops to a no-memory fallback agent when
the import fails. A dependency graph that omits the session manager would
therefore run every memory-enabled request without memory and emit only a
WARNING log. This test fails loudly in that case so the gap is caught in CI
instead of in production.
"""

# Standard library
import importlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_SESSION_MANAGER_MODULE = "bedrock_agentcore.memory.integrations.strands.session_manager"
_SESSION_MANAGER_CLASS = "AgentCoreMemorySessionManager"


def test_memory_session_manager_module_is_importable():
    """The module the orchestrator imports under ImportError must import."""
    module = importlib.import_module(_SESSION_MANAGER_MODULE)
    assert module is not None


def test_memory_session_manager_class_is_available():
    """The session-manager class the orchestrator binds must be present."""
    module = importlib.import_module(_SESSION_MANAGER_MODULE)
    session_manager = getattr(module, _SESSION_MANAGER_CLASS, None)
    assert session_manager is not None, (
        f"{_SESSION_MANAGER_MODULE}.{_SESSION_MANAGER_CLASS} is unavailable; the "
        "orchestrator would fall back to an agent without memory."
    )
    assert isinstance(session_manager, type)


def test_orchestrator_reports_memory_integration_available():
    """With the integration importable, the orchestrator flags it available."""
    # Local modules
    import agents.orchestrator as orch

    assert orch.MEMORY_INTEGRATION_AVAILABLE is True
    assert orch.AgentCoreMemorySessionManager is not None
