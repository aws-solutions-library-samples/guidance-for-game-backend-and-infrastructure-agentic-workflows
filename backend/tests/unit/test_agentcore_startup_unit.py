#!/usr/bin/env python3
"""Unit tests for AgentCore startup and initialization."""

# Standard library
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from bedrock_agentcore.runtime.context import RequestContext
from botocore.exceptions import ClientError, NoCredentialsError

pytestmark = [pytest.mark.unit]


def _jwt_context(session_id="ctx-session"):
    return RequestContext(session_id=session_id, request_headers={"Authorization": "Bearer test-token"})


def _verified_identity(subject="trusted-user"):
    # Local modules
    from runtime_identity import RuntimeIdentity

    return RuntimeIdentity(
        subject_id=subject,
        client_id="web-client",
        groups=frozenset({"users", "source-readers"}),
        scopes=frozenset({"openid", "profile"}),
    )


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
        """Hosted invocation derives authority from the verified Cognito JWT."""
        # Local modules
        import agentcore_main

        ctx = _jwt_context()
        with (
            patch.object(agentcore_main, "COST_SNAPSHOT_TABLE_NAME", "snap-table"),
            patch.object(agentcore_main, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
            patch.object(
                agentcore_main,
                "verify_cognito_runtime_identity",
                return_value=_verified_identity("subject-123"),
            ),
        ):
            result = agentcore_main.invoke_agent(
                {
                    "prompt": "test prompt",
                    "user_context": {
                        "user_id": "subject-123",
                        "groups": ["admin"],
                        "tenant": "attacker-tenant",
                        "workspace": "attacker-workspace",
                        "session_id": "test-session-456",
                    },
                },
                context=ctx,
            )

        assert result == "Test response"
        agent_context = mock_orchestrator.call_args.kwargs["context"]
        assert agent_context["session_id"] == "test-session-456"
        assert agent_context["actor_id"] == "subject-123"
        assert agent_context["client_id"] == "web-client"
        assert agent_context["audience"] == "web-client"
        assert agent_context["groups"] == ["source-readers", "users"]
        assert agent_context["scopes"] == ["openid", "profile"]
        assert agent_context["tenant"] == agentcore_main.DEPLOYMENT_TENANT_ID
        assert agent_context["workspace"] == agentcore_main.DEPLOYMENT_WORKSPACE_ID


class TestSharedModeScopeBoundary:
    """Shared-mode scope is established only by a verified Cognito token."""

    @pytest.fixture
    def mock_orchestrator(self):
        with patch("agentcore_main.run_orchestrator") as mock:
            mock.return_value = "Test response"
            yield mock

    @contextmanager
    def _shared_mode(self):
        module = __import__("agentcore_main")
        with (
            patch.object(module, "COST_SNAPSHOT_TABLE_NAME", "snap-table"),
            patch.object(module, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
        ):
            yield

    def test_shared_mode_uses_verified_jwt_actor(self, mock_orchestrator):
        # Local modules
        import agentcore_main

        with (
            self._shared_mode(),
            patch.object(
                agentcore_main,
                "verify_cognito_runtime_identity",
                return_value=_verified_identity("trusted-user"),
            ),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                context=_jwt_context(),
            )

        assert result == "Test response"
        mock_orchestrator.assert_called_once()

    def test_shared_mode_rejects_actor_mismatch(self, mock_orchestrator):
        # Local modules
        import agentcore_main

        with (
            self._shared_mode(),
            patch.object(
                agentcore_main,
                "verify_cognito_runtime_identity",
                return_value=_verified_identity("trusted-user"),
            ),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "someone-else"}},
                context=_jwt_context(),
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_shared_mode_requires_verified_jwt(self, mock_orchestrator):
        # Local modules
        import agentcore_main
        from runtime_identity import RuntimeIdentityError

        with (
            self._shared_mode(),
            patch.object(
                agentcore_main,
                "verify_cognito_runtime_identity",
                side_effect=RuntimeIdentityError(),
            ),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "body-only"}},
                context=RequestContext(session_id="s", request_headers={}),
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_shared_mode_rejects_custom_actor_passthrough_header(self, mock_orchestrator):
        # Local modules
        import agentcore_main
        from runtime_identity import RuntimeIdentityError

        context = RequestContext(
            session_id="s",
            request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id": "attacker"},
        )
        with (
            self._shared_mode(),
            patch.object(
                agentcore_main,
                "verify_cognito_runtime_identity",
                side_effect=RuntimeIdentityError(),
            ),
        ):
            result = agentcore_main.invoke_agent({"prompt": "hi", "user_context": {}}, context=context)

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_hosted_jwt_verification_does_not_depend_on_shared_snapshots(self, mock_orchestrator):
        # Local modules
        import agentcore_main

        verifier = MagicMock(return_value=_verified_identity("trusted-user"))
        with (
            patch.object(agentcore_main, "COST_SNAPSHOT_STORE_REQUIRED", False),
            patch.object(agentcore_main, "COST_SNAPSHOT_TABLE_NAME", ""),
            patch.object(agentcore_main, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
            patch.object(agentcore_main, "COGNITO_ISSUER", "https://issuer.example"),
            patch.object(agentcore_main, "COGNITO_CLIENT_ID", "web-client"),
            patch.object(agentcore_main, "verify_cognito_runtime_identity", verifier),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                context=_jwt_context(),
            )

        assert result == "Test response"
        verifier.assert_called_once()
        mock_orchestrator.assert_called_once()

    @pytest.mark.parametrize(
        ("issuer", "client_id"),
        [("", ""), ("https://issuer.example", ""), ("", "web-client")],
        ids=["both-missing", "client-missing", "issuer-missing"],
    )
    def test_hosted_mode_rejects_missing_jwt_bindings(self, mock_orchestrator, issuer, client_id):
        # Local modules
        import agentcore_main

        with (
            patch.object(agentcore_main, "COST_SNAPSHOT_STORE_REQUIRED", False),
            patch.object(agentcore_main, "COST_SNAPSHOT_TABLE_NAME", ""),
            patch.object(agentcore_main, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
            patch.object(agentcore_main, "COGNITO_ISSUER", issuer),
            patch.object(agentcore_main, "COGNITO_CLIENT_ID", client_id),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "body-only"}},
                context=_jwt_context(),
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_hosted_runtime_ignores_local_identity_bypass_flag(self, mock_orchestrator):
        # Local modules
        import agentcore_main
        from runtime_identity import RuntimeIdentityError

        with (
            patch.object(agentcore_main, "HOSTED_RUNTIME", True),
            patch.object(agentcore_main, "ALLOW_LOCAL_IDENTITY_BYPASS", True),
            patch.object(
                agentcore_main,
                "verify_cognito_runtime_identity",
                side_effect=RuntimeIdentityError(),
            ),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "body-only"}},
                context=RequestContext(session_id="s", request_headers={}),
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()

    def test_hosted_jwt_rejects_user_without_approved_group(self, mock_orchestrator):
        # Local modules
        import agentcore_main
        from runtime_identity import RuntimeIdentity

        unapproved = RuntimeIdentity(
            subject_id="trusted-user",
            client_id="web-client",
            groups=frozenset(),
            scopes=frozenset({"openid"}),
        )
        with (
            self._shared_mode(),
            patch.object(agentcore_main, "verify_cognito_runtime_identity", return_value=unapproved),
        ):
            result = agentcore_main.invoke_agent(
                {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                context=_jwt_context(),
            )

        assert "identity verification" in result.lower()
        mock_orchestrator.assert_not_called()


class TestRequestScopeLifecycle:
    """The request scope must be reset after each invocation, including on error."""

    @contextmanager
    def _shared_mode(self):
        module = __import__("agentcore_main")
        with (
            patch.object(module, "COST_SNAPSHOT_TABLE_NAME", "snap-table"),
            patch.object(module, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
        ):
            yield

    def test_scope_reset_after_successful_invocation(self):
        # Local modules
        from agentcore_main import invoke_agent
        from agents.cost_report_scope import UNSCOPED, current_scope

        with (
            patch("agentcore_main.run_orchestrator", return_value="ok") as mock,
            patch("agentcore_main.verify_cognito_runtime_identity", return_value=_verified_identity()),
        ):
            with self._shared_mode():
                invoke_agent(
                    {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                    context=_jwt_context(),
                )
            mock.assert_called_once()

        # After the call returns, the boundary scope must be restored to UNSCOPED
        # so it never leaks to the next request on the same worker.
        assert current_scope() is UNSCOPED

    def test_scope_reset_when_orchestrator_raises(self):
        # Local modules
        from agentcore_main import invoke_agent
        from agents.cost_report_scope import UNSCOPED, current_scope

        with (
            patch("agentcore_main.run_orchestrator", side_effect=RuntimeError("boom")),
            patch("agentcore_main.verify_cognito_runtime_identity", return_value=_verified_identity()),
        ):
            with self._shared_mode():
                result = invoke_agent(
                    {"prompt": "hi", "user_context": {"user_id": "trusted-user"}},
                    context=_jwt_context(),
                )

        # The exception is handled and a generic error returned...
        assert "error" in result.lower()
        # ...and the scope is still reset via the finally block.
        assert current_scope() is UNSCOPED
