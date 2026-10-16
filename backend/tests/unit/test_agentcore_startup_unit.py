#!/usr/bin/env python3
"""Unit tests for AgentCore startup and initialization."""

# Standard library
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from bedrock_agentcore.runtime.context import RequestContext
from botocore.exceptions import ClientError, NoCredentialsError

# Local modules
from _guard_state import telemetry_guard_isolation

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolate_telemetry_guard_state():
    """Snapshot and restore the content guard's process-wide state per test.

    Tests in this module import ``agentcore_main``, which runs the telemetry
    content guard against the real environment and installs process-wide Strands
    tracer wrappers and an MCP logger filter. This fixture restores those after
    each test so the guard's installs do not leak into the rest of the session.
    """
    yield from telemetry_guard_isolation()


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


class TestEntrypointTelemetryGuardWiring:
    """Pin that the entrypoint applies the content guard before any MCP client.

    The guard overrides the distro's content switches and uninstruments the MCP
    instrumentor; both must happen before ``prewarm_container`` creates the first
    MCP client, or MCP tool-content spans would already be wired. Importing the
    entrypoint fresh exercises that ordering. Removing the guard call from the
    entrypoint fails these assertions.
    """

    def _fresh_import_with_recording(self):
        # Standard library
        import importlib
        import logging
        import os
        import sys
        from unittest.mock import MagicMock, patch

        # Third-party packages
        from amazon.opentelemetry.distro.instrumentation.mcp import McpInstrumentor
        from mcp.shared.session import BaseSession
        from strands.telemetry.tracer import Tracer

        # Local modules
        from config.telemetry_guard import MCP_EXCEPTION_LOGGER_NAMES, _McpExceptionInfoFilter

        # Snapshot the Strands Tracer methods and the MCP logger filters so the
        # guard's process-wide installs during the fresh import are reverted and
        # do not leak into the rest of the session.
        saved_start_agent_span = Tracer.start_agent_span
        saved_end_span = Tracer._end_span
        saved_filters = {name: list(logging.getLogger(name).filters) for name in MCP_EXCEPTION_LOGGER_NAMES}

        # Start from a known single-instrumented MCP state.
        instrumentor = McpInstrumentor()
        for _ in range(5):
            if not hasattr(BaseSession.send_request, "__wrapped__"):
                break
            instrumentor._is_instrumented_by_opentelemetry = True
            instrumentor.uninstrument()
        instrumentor._is_instrumented_by_opentelemetry = False
        instrumentor.instrument()

        recorded = {}

        def _recording_create_mcp_client(server_name, *args, **kwargs):
            # Capture the MCP wrapping state and the content switch at the moment
            # the first client would be created during prewarm.
            if "mcp_wrapped_at_first_client" not in recorded:
                recorded["mcp_wrapped_at_first_client"] = hasattr(BaseSession.send_request, "__wrapped__")
                recorded["capture_env_at_first_client"] = os.environ.get(
                    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
                )
            return MagicMock()

        # Remove any OTEL switches so a missing guard call is detectable.
        preserved = {
            k: os.environ.pop(k, None)
            for k in (
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
                "OTEL_SEMCONV_STABILITY_OPT_IN",
                "GBAW_CAPTURE_TRACE_CONTENT",
            )
        }
        sys.modules.pop("agentcore_main", None)
        try:
            with (
                patch("boto3.client") as mock_boto3,
                patch("utils.mcp_client_factory.create_mcp_client", side_effect=_recording_create_mcp_client),
                patch("models.cached_bedrock.create_cached_bedrock_model", return_value=MagicMock()),
                patch("utils.kb_tools.create_kb_retrieve_tool", return_value=MagicMock()),
            ):
                mock_sts = MagicMock()
                mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
                mock_boto3.return_value = mock_sts
                importlib.import_module("agentcore_main")
            final_capture = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
            final_mcp_wrapped = hasattr(BaseSession.send_request, "__wrapped__")
        finally:
            # Restore environment and MCP state for the rest of the session.
            for k, v in preserved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for _ in range(5):
                if not hasattr(BaseSession.send_request, "__wrapped__"):
                    break
                instrumentor._is_instrumented_by_opentelemetry = True
                instrumentor.uninstrument()
            instrumentor._is_instrumented_by_opentelemetry = False
            sys.modules.pop("agentcore_main", None)
            # Restore the Strands Tracer methods and the MCP logger filters the
            # guard installed during the import, so they do not leak into the
            # rest of the session.
            Tracer.start_agent_span = saved_start_agent_span
            Tracer._end_span = saved_end_span
            for name, flts in saved_filters.items():
                lg = logging.getLogger(name)
                for flt in list(lg.filters):
                    if flt not in flts:
                        lg.removeFilter(flt)

        recorded["final_capture"] = final_capture
        recorded["final_mcp_wrapped"] = final_mcp_wrapped
        return recorded

    def test_guard_applied_and_mcp_unwrapped_before_first_client(self):
        # ``mcp`` and ``aws-opentelemetry-distro`` are direct dependencies, so the
        # ADOT MCP instrumentor must import. This test imports it directly (no
        # importorskip): if the ADOT MCP module moves or is renamed upstream, the
        # import fails and this required check fails rather than skipping, which is
        # how CI catches ADOT MCP drift before a deploy leaves the MCP tool-content
        # path open.
        recorded = self._fresh_import_with_recording()

        # The entrypoint created at least one MCP client during prewarm.
        assert "mcp_wrapped_at_first_client" in recorded
        # The content switch was applied before that client was created.
        assert recorded["capture_env_at_first_client"] == "false"
        # The MCP instrumentor was uninstrumented before that client was created.
        assert recorded["mcp_wrapped_at_first_client"] is False
        # And the module import left the redacting default in place.
        assert recorded["final_capture"] == "false"
        assert recorded["final_mcp_wrapped"] is False

    def test_fresh_import_does_not_leak_guard_state_into_session(self):
        # Importing the entrypoint runs the guard against the real os.environ and
        # installs process-wide Strands wrappers and MCP logger filters. This test
        # proves the fresh-import harness snapshots and restores all of them, so
        # later tests in the session do not inherit the redaction switches, the
        # tracer wrappers, or the logging filters.
        #
        # The test first forces a clean baseline — guard variables removed, the
        # tracer methods unwrapped through ``__wrapped__``, and the guard's own
        # logging filters removed — so the assertions compare against a state the
        # guard definitely did not leave behind. That way the test fails whenever
        # the harness stops restoring, regardless of collection order (any earlier
        # test in the session may already have run the guard once).
        # Standard library
        import logging
        import os

        # Third-party packages
        # ``mcp`` and ``aws-opentelemetry-distro`` are direct dependencies; import
        # the ADOT MCP module directly so drift fails this check rather than
        # skipping it.
        from strands.telemetry.tracer import Tracer

        # Local modules
        from config.telemetry_guard import MCP_EXCEPTION_LOGGER_NAMES, _McpExceptionInfoFilter

        otel_vars = (
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
            "OTEL_SEMCONV_STABILITY_OPT_IN",
            "GBAW_CAPTURE_TRACE_CONTENT",
            "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS",
        )

        def _guard_filter_count():
            total = 0
            for name in MCP_EXCEPTION_LOGGER_NAMES:
                total += sum(
                    1
                    for flt in logging.getLogger(name).filters
                    if getattr(flt, "marker", None) == _McpExceptionInfoFilter.marker
                )
            return total

        def _unwrap(method):
            # Resolve past any guard wrapper to the underlying Strands method.
            while hasattr(method, "__wrapped__"):
                method = method.__wrapped__
            return method

        # Force a clean baseline the guard cannot already have produced.
        for var in otel_vars:
            os.environ.pop(var, None)
        Tracer.start_agent_span = _unwrap(Tracer.start_agent_span)
        Tracer._end_span = _unwrap(Tracer._end_span)
        for name in MCP_EXCEPTION_LOGGER_NAMES:
            lg = logging.getLogger(name)
            for flt in list(lg.filters):
                if getattr(flt, "marker", None) == _McpExceptionInfoFilter.marker:
                    lg.removeFilter(flt)

        env_before = {k: os.environ.get(k) for k in otel_vars}
        start_before = Tracer.start_agent_span
        end_before = Tracer._end_span
        filters_before = _guard_filter_count()
        # Baseline really is clean: no guard variables, no guard filters, and the
        # tracer methods carry no guard wrapper.
        assert env_before == {k: None for k in otel_vars}
        assert filters_before == 0
        assert not hasattr(start_before, "__wrapped__")
        assert not hasattr(end_before, "__wrapped__")

        self._fresh_import_with_recording()

        # The harness must leave the clean baseline exactly as it found it.
        assert {k: os.environ.get(k) for k in otel_vars} == env_before
        assert Tracer.start_agent_span is start_before
        assert Tracer._end_span is end_before
        assert _guard_filter_count() == filters_before
        assert not hasattr(Tracer.start_agent_span, "__wrapped__")
        assert not hasattr(Tracer._end_span, "__wrapped__")


class TestTraceContentPostureLine:
    """The startup posture line must match the resolved decision.

    A clean redacting run says exported telemetry carries no content; a run with
    a skipped guard path must not make that claim and must instead report, at
    WARNING, that redaction is incomplete and name the open path.
    """

    def test_clean_redaction_logs_no_content_at_info(self):
        # Local modules
        from agentcore_main import _log_trace_content_posture
        from config.telemetry_guard import TraceContentDecision

        decision = TraceContentDecision(
            capture_enabled=False,
            capture_message_content="false",
            semconv_opt_in="gen_ai_unredacted_attributes=",
            skipped_paths=(),
        )
        log = MagicMock()

        _log_trace_content_posture(decision, log)

        log.info.assert_called_once()
        log.warning.assert_not_called()
        assert "carries no prompt or response content" in log.info.call_args.args[0]

    def test_skipped_path_logs_incomplete_at_warning_and_names_the_path(self):
        # Local modules
        from agentcore_main import _log_trace_content_posture
        from config.telemetry_guard import TraceContentDecision

        decision = TraceContentDecision(
            capture_enabled=False,
            capture_message_content="false",
            semconv_opt_in="gen_ai_unredacted_attributes=",
            skipped_paths=("strands span wrappers",),
        )
        log = MagicMock()

        _log_trace_content_posture(decision, log)

        # It must warn, name the open path, and never claim no content is exported.
        log.warning.assert_called_once()
        log.info.assert_not_called()
        rendered = log.warning.call_args.args[0] % log.warning.call_args.args[1:]
        assert "redaction incomplete" in rendered.lower()
        assert "strands span wrappers" in rendered
        assert "carries no prompt or response content" not in rendered

    def test_opt_in_logs_capture_enabled_at_warning(self):
        # Local modules
        from agentcore_main import _log_trace_content_posture
        from config.telemetry_guard import TraceContentDecision

        decision = TraceContentDecision(
            capture_enabled=True,
            capture_message_content="true",
            semconv_opt_in="",
            skipped_paths=(),
        )
        log = MagicMock()

        _log_trace_content_posture(decision, log)

        log.warning.assert_called_once()
        assert "capture is ENABLED" in log.warning.call_args.args[0]
