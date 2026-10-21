#!/usr/bin/env python3
"""Unit tests for Billing MCP subprocess log suppression (#465).

The AWS Labs ``billing-cost-management-mcp-server`` configures its loguru stderr
AND file sinks at ``FASTMCP_LOG_LEVEL`` (default ``INFO``) and logs raw provider
error bodies — including ARNs and account IDs — at ``INFO``/``ERROR``.
``mcp_wrapper.py`` forwards that child stderr to the parent, so those bodies
would reach the runtime's logs. The factory therefore pins the Billing
subprocess environment to ``FASTMCP_LOG_LEVEL=CRITICAL`` and
``LOGURU_LEVEL=CRITICAL`` so the server's own sinks never emit provider-authored
error text.

These tests observe the ACTUAL environment the factory builds for the Billing
subprocess (by capturing the ``StdioServerParameters`` passed to the transport),
and prove against the INSTALLED server that those variables silence its loguru
sinks. They live in a dedicated file so they do not collide with the separately
owned ``test_mcp_client_factory_behavior_unit.py``.
"""

# Standard library
import importlib
import io
import os
import sys
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from utils.mcp_client_factory import clear_mcp_cache, create_mcp_client

# Synthetic hostile value: an account id must never reach a parent log stream.
_ACCT = "123456789012"
_ARN = f"arn:aws:sts::{_ACCT}:assumed-role/role/session"


def _capture_subprocess_env(server_name: str) -> dict:
    """Create a client for ``server_name`` and return the subprocess env dict.

    ``MCPClient(factory)`` does not call its transport factory eagerly, so we
    patch ``MCPClient`` to invoke the factory immediately and
    ``StdioServerParameters`` to record the ``env`` it receives.
    """
    captured: dict = {}

    class _FakeParams:
        def __init__(self, *args, **kwargs):
            if "env" in kwargs:
                captured.update(kwargs["env"])

    def _fake_stdio_client(*_args, **_kwargs):
        return object()

    def _fake_mcp_client(factory):
        # Force the transport factory so StdioServerParameters is constructed.
        factory()
        return object()

    clear_mcp_cache()
    with (
        patch("utils.mcp_client_factory.StdioServerParameters", _FakeParams),
        patch("utils.mcp_client_factory.stdio_client", _fake_stdio_client),
        patch("utils.mcp_client_factory.MCPClient", _fake_mcp_client),
    ):
        create_mcp_client(server_name, use_cache=False)
    return captured


class TestBillingSubprocessEnvPinsCriticalLogging:
    def test_billing_env_sets_fastmcp_and_loguru_level_critical(self):
        env = _capture_subprocess_env("billing-cost-management-mcp-server")
        assert env.get("FASTMCP_LOG_LEVEL") == "CRITICAL"
        assert env.get("LOGURU_LEVEL") == "CRITICAL"

    def test_billing_env_still_pins_writable_db_and_log_paths(self):
        # The pre-existing read-only-FS relocations must remain intact.
        env = _capture_subprocess_env("billing-cost-management-mcp-server")
        assert "GBAW_BILLING_MCP_DB_DIR" in env
        assert env.get("FASTMCP_LOG_FILE", "").endswith("billing-cost-management-mcp-server.log")


class TestInstalledBillingServerHonorsCriticalLevel:
    """Prove, against the installed server, that FASTMCP_LOG_LEVEL=CRITICAL
    silences BOTH loguru sinks the server configures (stderr + file)."""

    def _reload_logging_utils(self):
        # Third-party packages
        import awslabs.billing_cost_management_mcp_server.utilities.logging_utils as lu

        return importlib.reload(lu)

    def test_module_log_level_follows_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FASTMCP_LOG_LEVEL", "CRITICAL")
        monkeypatch.setenv("FASTMCP_LOG_FILE", str(tmp_path / "billing.log"))
        lu = self._reload_logging_utils()
        try:
            assert lu.LOG_LEVEL == "CRITICAL"
        finally:
            # Restore the module to its default-env state for other tests.
            monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
            importlib.reload(lu)

    def test_critical_level_suppresses_raw_provider_error_on_both_sinks(self, tmp_path, monkeypatch):
        log_file = tmp_path / "billing.log"
        monkeypatch.setenv("FASTMCP_LOG_LEVEL", "CRITICAL")
        monkeypatch.setenv("FASTMCP_LOG_FILE", str(log_file))
        lu = self._reload_logging_utils()
        # Third-party packages
        from loguru import logger as _loguru_logger

        stderr_buf = io.StringIO()
        try:
            # Reconfigure the shared loguru logger exactly as the server does,
            # but route the stderr sink into a buffer we can inspect. Both sinks
            # take their level from the env-derived module global.
            _loguru_logger.remove()
            _loguru_logger.add(stderr_buf, level=lu.LOG_LEVEL)
            _loguru_logger.add(str(log_file), level=lu.LOG_LEVEL)
            # The server logs raw provider errors at ERROR/INFO; both are below
            # CRITICAL and must be dropped.
            _loguru_logger.error(f"Error in Cost Explorer operation 'GetCostForecast': {_ARN}")
            _loguru_logger.info(f"Retrieved recommendations for {_ARN}")
            _loguru_logger.complete()
        finally:
            _loguru_logger.remove()
            monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
            importlib.reload(lu)

        assert _ARN not in stderr_buf.getvalue()
        assert _ACCT not in stderr_buf.getvalue()
        file_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert _ARN not in file_text
        assert _ACCT not in file_text
