"""Each invocation keeps its own AgentCore Memory session unless the caller names one (#743).

The frontend sends one per-thread ID as ``user_context.session_id``, ``thread_id``,
and the AgentCore runtime session header. A caller that sent neither used to fall
back to the constant memory session ``"default"``. Every such invocation of one
user then shared a single conversation, and concurrent invocations replayed each
other's unanswered questions, so the orchestrator answered several at once.
"""

# Standard library
import re
import uuid
from unittest.mock import patch

# Third-party packages
import pytest
from bedrock_agentcore.runtime.context import RequestContext

# Local modules
from _guard_state import telemetry_guard_isolation
from utils.memory_identity import memory_actor_id, memory_session_id_for_runtime_session

pytestmark = [pytest.mark.unit]

# The AgentCore Memory CreateEvent sessionId rule from the locked botocore model.
_MEMORY_SESSION_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}")

# Synthetic runtime session IDs (AgentCore requires at least 33 characters).
RUNTIME_SESSION = "synthetic-runtime-session-0123456789abcdef0123"
OTHER_RUNTIME_SESSION = "synthetic-runtime-session-fedcba9876543210fedc"


@pytest.fixture(autouse=True)
def _isolate_telemetry_guard_state():
    """Restore the content guard's process-wide state after importing agentcore_main."""
    yield from telemetry_guard_isolation()


class TestRuntimeSessionMapping:
    def test_maps_to_a_valid_opaque_memory_session_id(self):
        session = memory_session_id_for_runtime_session(RUNTIME_SESSION)

        assert _MEMORY_SESSION_ID.fullmatch(session)
        assert session.startswith("rt-")
        assert RUNTIME_SESSION not in session

    def test_is_deterministic_per_runtime_session(self):
        assert memory_session_id_for_runtime_session(RUNTIME_SESSION) == memory_session_id_for_runtime_session(
            RUNTIME_SESSION
        )
        assert memory_session_id_for_runtime_session(RUNTIME_SESSION) != memory_session_id_for_runtime_session(
            OTHER_RUNTIME_SESSION
        )

    def test_is_domain_separated_from_actor_ids(self):
        assert memory_session_id_for_runtime_session(RUNTIME_SESSION).removeprefix("rt-") != memory_actor_id(
            RUNTIME_SESSION
        )

    @pytest.mark.parametrize("raw", ["a" * 256, "session:with.dots/and spaces", "-leading-dash-session-0000000000000"])
    def test_long_or_unusual_runtime_sessions_still_map_to_valid_ids(self, raw):
        assert _MEMORY_SESSION_ID.fullmatch(memory_session_id_for_runtime_session(raw))

    @pytest.mark.parametrize("bad", [None, "", "   ", 42])
    def test_unusable_runtime_session_raises(self, bad):
        with pytest.raises(ValueError):
            memory_session_id_for_runtime_session(bad)


def _verified_identity():
    # Local modules
    from runtime_identity import RuntimeIdentity

    # A unique synthetic subject per invocation keeps the per-user rate limiter out of the way.
    return RuntimeIdentity(
        subject_id=f"session-test-{uuid.uuid4().hex}",
        client_id="web-client",
        groups=frozenset({"users"}),
        scopes=frozenset({"openid"}),
    )


class TestInvokeAgentMemorySession:
    """``invoke_agent`` passes the orchestrator one memory session per conversation."""

    @pytest.fixture
    def mock_orchestrator(self):
        with patch("agentcore_main.run_orchestrator") as mock:
            mock.return_value = "Test response"
            yield mock

    @staticmethod
    def _invoke(payload, runtime_session):
        # Local modules
        import agentcore_main

        context = RequestContext(session_id=runtime_session, request_headers={"Authorization": "Bearer test-token"})
        with (
            patch.object(agentcore_main, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
            patch.object(agentcore_main, "verify_cognito_runtime_identity", return_value=_verified_identity()),
        ):
            assert agentcore_main.invoke_agent(payload, context=context) == "Test response"

    @staticmethod
    def _session(mock_orchestrator):
        return mock_orchestrator.call_args.kwargs["context"]["session_id"]

    def test_sessionless_invocation_uses_its_runtime_session_not_a_shared_default(self, mock_orchestrator):
        self._invoke({"prompt": "List my GameLift fleets."}, RUNTIME_SESSION)

        session = self._session(mock_orchestrator)
        assert session != "default"
        assert session == memory_session_id_for_runtime_session(RUNTIME_SESSION)

    def test_concurrent_sessionless_invocations_get_separate_memory_sessions(self, mock_orchestrator):
        self._invoke({"prompt": "List my GameLift fleets."}, RUNTIME_SESSION)
        first = self._session(mock_orchestrator)
        self._invoke({"prompt": "List my EKS clusters."}, OTHER_RUNTIME_SESSION)
        second = self._session(mock_orchestrator)

        assert first != second

    def test_repeat_invocations_in_one_runtime_session_keep_one_conversation(self, mock_orchestrator):
        self._invoke({"prompt": "List my GameLift fleets."}, RUNTIME_SESSION)
        first = self._session(mock_orchestrator)
        self._invoke({"prompt": "Which one is active?"}, RUNTIME_SESSION)

        assert self._session(mock_orchestrator) == first

    def test_frontend_session_id_still_takes_precedence(self, mock_orchestrator):
        self._invoke(
            {
                "prompt": "List my GameLift fleets.",
                "thread_id": "prod-thread-1",
                "user_context": {"session_id": "prod-thread-1"},
            },
            RUNTIME_SESSION,
        )

        assert self._session(mock_orchestrator) == "prod-thread-1"

    def test_thread_id_alone_still_names_the_session(self, mock_orchestrator):
        self._invoke({"prompt": "List my GameLift fleets.", "thread_id": "prod-thread-2"}, RUNTIME_SESSION)

        assert self._session(mock_orchestrator) == "prod-thread-2"

    def test_invocation_without_any_session_gets_a_unique_request_session(self, mock_orchestrator):
        self._invoke({"prompt": "List my GameLift fleets."}, None)
        first = self._session(mock_orchestrator)
        self._invoke({"prompt": "List my GameLift fleets."}, None)
        second = self._session(mock_orchestrator)

        assert first != "default" and second != "default"
        assert first != second
        assert _MEMORY_SESSION_ID.fullmatch(first) and _MEMORY_SESSION_ID.fullmatch(second)
