"""The AgentCore SDK log handler must not emit the raw session ID (#472).

``BedrockAgentCoreApp`` installs a JSON log handler on the
``bedrock_agentcore.app`` logger whose formatter writes the runtime session ID
(set by the frontend to the environment-isolated thread ID) on every invocation
record. ``agentcore_main`` wraps that formatter so ``sessionId`` is redacted
while ``requestId`` is preserved. This test drives the real SDK handler with a
synthetic raw session marker and asserts the marker never reaches the stream.
"""

# Standard library
import io
import json
import logging
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

RAW_SESSION_MARKER = "prod-thread-ZZRAW-0000000000000000000000000"
REQUEST_ID = "req-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _import_agentcore_main():
    with patch("boto3.client") as mock_boto:
        mock_boto.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
        # Local modules
        import agentcore_main

    return agentcore_main


def test_sdk_handler_redacts_raw_session_id_but_keeps_request_id():
    # Third-party packages
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    agentcore_main = _import_agentcore_main()

    sdk_logger = logging.getLogger("bedrock_agentcore.app")
    # The SDK registers at least one handler at app construction; the wrapper is
    # installed by agentcore_main at import.
    assert sdk_logger.handlers, "expected the SDK to register a handler"

    # Re-point each handler at an in-memory stream so we can read what it emits,
    # then re-apply the redacting wrapper (handler.formatter is preserved).
    buffer = io.StringIO()
    for handler in sdk_logger.handlers:
        handler.stream = buffer  # type: ignore[attr-defined]
    # Call the installer twice: the module-level wrapper type makes this
    # idempotent, so a second call must not stack a second wrapper (which would
    # re-tokenize an already-redacted sessionId).
    agentcore_main._install_sdk_session_redaction()
    agentcore_main._install_sdk_session_redaction()

    BedrockAgentCoreContext.set_request_context(REQUEST_ID, RAW_SESSION_MARKER)
    try:
        sdk_logger.info("Invocation completed successfully")
    finally:
        BedrockAgentCoreContext.set_request_context(REQUEST_ID, None)

    output = buffer.getvalue()
    assert output, "the SDK handler emitted nothing"
    assert RAW_SESSION_MARKER not in output, "raw session id reached the SDK log stream"

    # The emitted record keeps a (redacted) sessionId and the raw requestId.
    last_line = [line for line in output.splitlines() if line.strip()][-1]
    payload = json.loads(last_line)
    assert payload.get("requestId") == REQUEST_ID
    assert payload.get("sessionId", "").startswith("id:")


def test_sdk_session_redaction_is_idempotent():
    """A repeated install must leave the redaction token stable, matching the
    application's ``Session:`` line (a double-wrap would change it)."""
    # Third-party packages
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    agentcore_main = _import_agentcore_main()
    sdk_logger = logging.getLogger("bedrock_agentcore.app")

    def _emit() -> str:
        buffer = io.StringIO()
        for handler in sdk_logger.handlers:
            handler.stream = buffer  # type: ignore[attr-defined]
        BedrockAgentCoreContext.set_request_context(REQUEST_ID, RAW_SESSION_MARKER)
        try:
            sdk_logger.info("Invocation completed successfully")
        finally:
            BedrockAgentCoreContext.set_request_context(REQUEST_ID, None)
        last_line = [line for line in buffer.getvalue().splitlines() if line.strip()][-1]
        return json.loads(last_line).get("sessionId", "")

    agentcore_main._install_sdk_session_redaction()
    first = _emit()
    agentcore_main._install_sdk_session_redaction()
    second = _emit()

    assert first.startswith("id:")
    assert first == second, "a repeated install double-wrapped the formatter and changed the token"
