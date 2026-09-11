#!/usr/bin/env python3
"""Unit tests for AgentCore startup and initialization."""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from bedrock_agentcore.runtime.context import RequestContext
from botocore.exceptions import ClientError, NoCredentialsError
from starlette.requests import Request

pytestmark = [pytest.mark.unit]

RUNTIME_USER_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-User-Id"


def _context_with_forwarded_header(user_id, header_name=RUNTIME_USER_ID_HEADER, session_id="ctx-session"):
    """Build a real RequestContext whose forwarded header map carries a user id.

    This mirrors the SDK ``RequestContext`` shape (``request_headers`` is the
    forwarded header dict) rather than inventing Mock attributes.
    """
    return RequestContext(session_id=session_id, request_headers={header_name: user_id})


def _context_with_underlying_request(user_id, header_name=RUNTIME_USER_ID_HEADER, session_id="ctx-session"):
    """Build a real RequestContext backed by a Starlette request carrying the header.

    The runtime allowlist does not forward the reserved User-Id header into
    ``request_headers``, so the deployed path reads it from the underlying
    Starlette request object. Starlette header keys are wire-lowercased.
    """
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/invocations",
        "headers": [(header_name.lower().encode("latin-1"), user_id.encode("latin-1"))],
    }
    return RequestContext(session_id=session_id, request=Request(scope))


class TestAgentCoreStartup:
    """Test AgentCore startup and AWS credentials validation."""

    def test_aws_credentials_valid(self):
        """Test successful AWS credentials validation."""
        with patch("boto3.client") as mock_boto3:
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
            mock_boto3.return_value = mock_sts

            # Local modules
            from agentcore_main import validate_aws_credentials

            result = validate_aws_credentials()

            assert result is True

    def test_aws_credentials_invalid_logs_error(self):
        """Test that invalid AWS credentials log error but don't crash."""
        with patch("boto3.client") as mock_boto3:
            mock_boto3.side_effect = NoCredentialsError()

            # Local modules
            from agentcore_main import validate_aws_credentials

            result = validate_aws_credentials()

            assert result is False

    def test_aws_credentials_expired_logs_error(self):
        """Test that expired AWS credentials log error but don't crash."""
        with patch("boto3.client") as mock_boto3:
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.side_effect = ClientError(
                {"Error": {"Code": "ExpiredToken", "Message": "Token expired"}}, "GetCallerIdentity"
            )
            mock_boto3.return_value = mock_sts

            # Local modules
            from agentcore_main import validate_aws_credentials

            result = validate_aws_credentials()

            assert result is False

    def test_aws_credentials_network_error_logs_error(self):
        """Test that network errors during credential check log error but don't crash."""
        with patch("boto3.client") as mock_boto3:
            mock_boto3.side_effect = Exception("Network timeout")

            # Local modules
            from agentcore_main import validate_aws_credentials

            result = validate_aws_credentials()

            assert result is False


class TestInvokeAgent:
    """Test the invoke_agent entrypoint function."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Mock run_orchestrator."""
        with patch("agentcore_main.run_orchestrator") as mock:
            mock.return_value = "Test response"
            yield mock

    def test_invoke_agent_with_string_prompt(self, mock_orchestrator):
        """Test invoke_agent with string prompt."""
        # Local modules
        from agentcore_main import invoke_agent

        result = invoke_agent("test prompt")

        assert result == "Test response"
        mock_orchestrator.assert_called_once()
        call_args = mock_orchestrator.call_args
        assert call_args.kwargs["query"] == "test prompt"

    def test_invoke_agent_with_dict_prompt(self, mock_orchestrator):
        """Test invoke_agent with dict prompt."""
        # Local modules
        from agentcore_main import invoke_agent

        result = invoke_agent({"prompt": "test prompt"})

        assert result == "Test response"

    def test_invoke_agent_returns_readiness_message_when_credentials_unavailable(self, mock_orchestrator):
        """If startup creds failed AND the re-check still fails, return a clear
        'initializing' message instead of calling the orchestrator (#123)."""
        # Local modules
        import agentcore_main

        with (
            patch.object(agentcore_main, "_CREDENTIALS_OK", False),
            patch.object(agentcore_main, "validate_aws_credentials", return_value=False),
        ):
            result = agentcore_main.invoke_agent("test prompt")

        assert "initializing" in result.lower()
        # Must short-circuit — orchestrator never called when not ready.
        mock_orchestrator.assert_not_called()

    def test_invoke_agent_recovers_when_credentials_become_available(self, mock_orchestrator):
        """If startup creds failed but the lazy re-check succeeds, proceed."""
        # Local modules
        import agentcore_main

        with (
            patch.object(agentcore_main, "_CREDENTIALS_OK", False),
            patch.object(agentcore_main, "validate_aws_credentials", return_value=True),
        ):
            result = agentcore_main.invoke_agent("test prompt")

        assert result == "Test response"
        mock_orchestrator.assert_called_once()
        assert mock_orchestrator.call_args.kwargs["query"] == "test prompt"

    def test_invoke_agent_with_empty_dict(self, mock_orchestrator):
        """Test invoke_agent with empty dict returns validation error.

        Security: Empty prompts are now rejected by input validation.
        """
        # Local modules
        from agentcore_main import invoke_agent

        result = invoke_agent({})

        # Security validation now rejects empty prompts
        assert "couldn't process your request" in result
        assert "cannot be empty" in result
        # Orchestrator should NOT be called for invalid input
        mock_orchestrator.assert_not_called()

    def test_invoke_agent_handles_exception(self, mock_orchestrator):
        """Test invoke_agent handles exceptions gracefully."""
        # Local modules
        from agentcore_main import invoke_agent

        mock_orchestrator.side_effect = Exception("Test error")

        result = invoke_agent("test prompt")

        assert "error" in result.lower()
        assert "try again" in result.lower()

    def test_invoke_agent_with_context(self, mock_orchestrator):
        """Test invoke_agent derives actor_id from the trusted runtimeUserId header."""
        # Local modules
        from agentcore_main import invoke_agent

        # Real RequestContext carrying the platform-established runtime user id.
        ctx = _context_with_forwarded_header("test-user")

        result = invoke_agent(
            {
                "prompt": "test prompt",
                "user_context": {
                    "user_id": "test-user-123",
                    "client_id": "web-client",
                    "audience": "web-client",
                    "groups": ["users", "source-readers"],
                    "scopes": ["openid", "profile"],
                    "tenant": "tenant-a",
                    "workspace": "workspace-a",
                    "session_id": "test-session-456",
                },
            },
            context=ctx,
        )

        assert result == "Test response"
        call_args = mock_orchestrator.call_args
        agent_context = call_args.kwargs["context"]
        # session_id should come from user_context.session_id
        assert agent_context["session_id"] == "test-session-456"
        # actor_id must be the trusted transport identity from the header.
        assert agent_context["actor_id"] == "test-user"
        assert agent_context["client_id"] == "web-client"
        assert agent_context["audience"] == "web-client"
        assert agent_context["groups"] == ["users", "source-readers"]
        assert agent_context["scopes"] == ["openid", "profile"]
        assert agent_context["tenant"] == "tenant-a"
        assert agent_context["workspace"] == "workspace-a"


class TestExtractRuntimeUserId:
    """Direct coverage of the trusted runtime-user-id extraction helper (#365).

    Exercised against the real ``RequestContext`` (forwarded header map) and a
    real Starlette request (underlying request object), plus case variants and
    the missing/blank cases — never invented Mock attributes.
    """

    def test_returns_none_when_context_is_none(self):
        # Local modules
        from agentcore_main import extract_runtime_user_id

        assert extract_runtime_user_id(None) is None

    def test_reads_from_forwarded_request_headers(self):
        # Local modules
        from agentcore_main import extract_runtime_user_id

        ctx = _context_with_forwarded_header("alice")
        assert extract_runtime_user_id(ctx) == "alice"

    def test_forwarded_header_lookup_is_case_insensitive(self):
        # Local modules
        from agentcore_main import extract_runtime_user_id

        ctx = _context_with_forwarded_header("bob", header_name="x-amzn-bedrock-agentcore-runtime-user-id")
        assert extract_runtime_user_id(ctx) == "bob"

        ctx_upper = _context_with_forwarded_header("carol", header_name="X-AMZN-BEDROCK-AGENTCORE-RUNTIME-USER-ID")
        assert extract_runtime_user_id(ctx_upper) == "carol"

    def test_falls_back_to_underlying_request_object(self):
        # Local modules
        from agentcore_main import extract_runtime_user_id

        # The reserved header is not forwarded into request_headers by the runtime
        # allowlist; it is only present on the underlying Starlette request.
        ctx = _context_with_underlying_request("dave")
        assert ctx.request_headers is None
        assert extract_runtime_user_id(ctx) == "dave"

    def test_missing_header_returns_none(self):
        # Local modules
        from agentcore_main import extract_runtime_user_id

        ctx = RequestContext(session_id="s", request_headers={"X-Other-Header": "x"})
        assert extract_runtime_user_id(ctx) is None

    def test_blank_header_value_returns_none(self):
        # Local modules
        from agentcore_main import extract_runtime_user_id

        ctx = _context_with_forwarded_header("   ")
        assert extract_runtime_user_id(ctx) is None

    def test_ignores_custom_actor_passthrough_header(self):
        """The caller-supplied custom passthrough header must NEVER be trusted as
        the runtime user identity."""
        # Local modules
        from agentcore_main import extract_runtime_user_id

        ctx = RequestContext(
            session_id="s",
            request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id": "attacker"},
        )
        assert extract_runtime_user_id(ctx) is None


class TestSharedModeScopeBoundary:
    """Shared-mode trusted-actor resolution at the AgentCore boundary (#365).

    All contexts are real ``RequestContext`` objects; the trusted actor is only
    ever the platform-established runtimeUserId header.
    """

    @pytest.fixture
    def mock_orchestrator(self):
        with patch("agentcore_main.run_orchestrator") as mock:
            mock.return_value = "Test response"
            yield mock

    def _shared_mode(self):
        # Force shared mode regardless of the local test environment.
        return patch.object(__import__("agentcore_main"), "COST_SNAPSHOT_TABLE_NAME", "snap-table")

    def test_shared_mode_uses_trusted_context_actor(self, mock_orchestrator):
        # Local modules
        from agentcore_main import invoke_agent

        ctx = _context_with_forwarded_header("trusted-user")

        with self._shared_mode():
            result = invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                context=ctx,
            )

        assert result == "Test response"
        mock_orchestrator.assert_called_once()

    def test_shared_mode_accepts_trusted_actor_from_underlying_request(self, mock_orchestrator):
        # Local modules
        from agentcore_main import invoke_agent

        ctx = _context_with_underlying_request("trusted-user")

        with self._shared_mode():
            result = invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                context=ctx,
            )

        assert result == "Test response"
        mock_orchestrator.assert_called_once()

    def test_shared_mode_rejects_actor_mismatch(self, mock_orchestrator):
        # Local modules
        from agentcore_main import invoke_agent

        ctx = _context_with_forwarded_header("trusted-user")

        with self._shared_mode():
            result = invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "someone-else"}},
                context=ctx,
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_shared_mode_requires_trusted_actor(self, mock_orchestrator):
        # Local modules
        from agentcore_main import invoke_agent

        # No runtimeUserId header present at all; a body-only identity is not trusted.
        ctx = RequestContext(session_id="s", request_headers={})

        with self._shared_mode():
            result = invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "body-only"}},
                context=ctx,
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_shared_mode_rejects_custom_actor_passthrough_header(self, mock_orchestrator):
        """A request that supplies ONLY the caller-controlled custom passthrough
        header (and no trusted runtimeUserId) must be rejected in shared mode: a
        custom passthrough header can never establish snapshot scope."""
        # Local modules
        from agentcore_main import invoke_agent

        ctx = RequestContext(
            session_id="s",
            request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id": "attacker"},
        )

        with self._shared_mode():
            result = invoke_agent({"prompt": "hi", "user_context": {}}, context=ctx)

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()


class TestRequestScopeLifecycle:
    """The request scope must be reset after each invocation, including on error."""

    def _shared_mode(self):
        return patch.object(__import__("agentcore_main"), "COST_SNAPSHOT_TABLE_NAME", "snap-table")

    def test_scope_reset_after_successful_invocation(self):
        # Local modules
        from agentcore_main import invoke_agent
        from agents.cost_report_scope import UNSCOPED, current_scope

        with patch("agentcore_main.run_orchestrator", return_value="ok") as mock:
            ctx = _context_with_forwarded_header("trusted-user")
            with self._shared_mode():
                invoke_agent({"prompt": "hi", "user_context": {"user_id": "trusted-user"}}, context=ctx)
            mock.assert_called_once()

        # After the call returns, the boundary scope must be restored to UNSCOPED
        # so it never leaks to the next request on the same worker.
        assert current_scope() is UNSCOPED

    def test_scope_reset_when_orchestrator_raises(self):
        # Local modules
        from agentcore_main import invoke_agent
        from agents.cost_report_scope import UNSCOPED, current_scope

        with patch("agentcore_main.run_orchestrator", side_effect=RuntimeError("boom")):
            ctx = _context_with_forwarded_header("trusted-user")
            with self._shared_mode():
                result = invoke_agent(
                    {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                    context=ctx,
                )

        # The exception is handled and a generic error returned...
        assert "error" in result.lower()
        # ...and the scope is still reset via the finally block.
        assert current_scope() is UNSCOPED
