#!/usr/bin/env python3
"""Unit tests for AgentCore startup and initialization."""

# Standard library
import sys
from unittest.mock import MagicMock, Mock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

pytestmark = [pytest.mark.unit]


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
        """Test invoke_agent extracts user_id and actor from context."""
        # Local modules
        from agentcore_main import invoke_agent

        mock_context = Mock()
        mock_context.user_id = "test-user"
        mock_context.headers = {}

        # Pass user_id in the prompt dict
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
            context=mock_context,
        )

        assert result == "Test response"
        call_args = mock_orchestrator.call_args
        agent_context = call_args.kwargs["context"]
        # session_id should come from user_context.session_id
        assert agent_context["session_id"] == "test-session-456"
        # actor_id should be from context.user_id
        assert agent_context["actor_id"] == "test-user"
        assert agent_context["client_id"] == "web-client"
        assert agent_context["audience"] == "web-client"
        assert agent_context["groups"] == ["users", "source-readers"]
        assert agent_context["scopes"] == ["openid", "profile"]
        assert agent_context["tenant"] == "tenant-a"
        assert agent_context["workspace"] == "workspace-a"
