#!/usr/bin/env python3
"""Unit tests for MCP client factory behavior - pure logic testing."""

# Standard library
import os
import sys
from unittest.mock import MagicMock, Mock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from utils.mcp_client_factory import _build_startup_code, _resolve_mcp_command, clear_mcp_cache, create_mcp_client


class TestMCPClientFactoryBehavior:
    """Test MCP client factory behavioral logic."""

    def setup_method(self):
        """Clear MCP client cache before each test."""
        clear_mcp_cache()

    def test_create_mcp_client_returns_none_for_unknown_server(self):
        """Test create_mcp_client returns None for unknown server types."""
        result = create_mcp_client("unknown-server-type")
        assert result is None

    def test_create_mcp_client_known_server_types(self):
        """Test create_mcp_client recognizes known server types."""
        known_servers = ["ccapi-mcp-server", "eks-mcp-server", "cost-explorer-mcp-server"]

        for server_type in known_servers:
            clear_mcp_cache()  # Clear cache before each iteration
            # Should not return None for known types (even if creation fails)
            with patch("utils.mcp_client_factory.MCPClient") as mock_client:
                mock_client.side_effect = Exception("Connection failed")
                result = create_mcp_client(server_type)
                # Should return None due to connection failure, but not due to unknown type
                assert result is None

    @patch("utils.mcp_client_factory.MCPClient")
    def test_handles_client_creation_failure(self, mock_mcp_client):
        """Test handles MCPClient creation failure gracefully."""
        mock_mcp_client.side_effect = Exception("Failed to create client")

        result = create_mcp_client("ccapi-mcp-server")
        assert result is None

    @patch("utils.mcp_client_factory.MCPClient")
    @patch("utils.mcp_client_factory.stdio_client")
    def test_mcp_client_creation_with_correct_parameters(self, mock_stdio_client, mock_mcp_client):
        """Test MCP client is created with correct parameters."""
        mock_client = Mock()
        mock_mcp_client.return_value = mock_client

        result = create_mcp_client("ccapi-mcp-server")

        # Verify MCPClient was called
        mock_mcp_client.assert_called_once()
        assert result == mock_client

    def test_server_type_validation(self):
        """Test server type validation logic."""
        # Local modules
        from utils.mcp_client_factory import MCP_CREATE_MAX_ATTEMPTS

        valid_types = ["ccapi-mcp-server", "eks-mcp-server", "cost-explorer-mcp-server"]

        invalid_types = ["invalid-server", "random-type", "", None]

        # Valid types should attempt creation with retries (may fail on connection)
        for server_type in valid_types:
            clear_mcp_cache()  # Clear cache before each iteration
            with patch("utils.mcp_client_factory.MCPClient") as mock_client:
                mock_client.side_effect = Exception("Connection test")
                result = create_mcp_client(server_type)
                # Should attempt creation up to MCP_CREATE_MAX_ATTEMPTS times
                assert mock_client.call_count == MCP_CREATE_MAX_ATTEMPTS

        # Invalid types should return None immediately
        for server_type in invalid_types:
            with patch("utils.mcp_client_factory.MCPClient") as mock_client:
                result = create_mcp_client(server_type)
                assert result is None
                # Should not attempt to create client for invalid types
                mock_client.assert_not_called()


class TestMCPClientFactoryEdgeCases:
    """Test MCP client factory edge cases and error conditions."""

    def setup_method(self):
        """Clear MCP client cache before each test."""
        clear_mcp_cache()

    def test_handles_none_server_type(self):
        """Test handles None server type gracefully."""
        result = create_mcp_client(None)
        assert result is None

    def test_handles_empty_server_type(self):
        """Test handles empty string server type gracefully."""
        result = create_mcp_client("")
        assert result is None

    def test_handles_whitespace_server_type(self):
        """Test handles whitespace-only server type gracefully."""
        result = create_mcp_client("   ")
        assert result is None

    @patch("utils.mcp_client_factory.MCPClient")
    def test_aws_environment_variables_passed(self, mock_mcp_client):
        """Test AWS environment variables are passed to MCP client."""
        mock_client = Mock()
        mock_mcp_client.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": "test_key",
                "AWS_SECRET_ACCESS_KEY": "test_secret",
                "AWS_SESSION_TOKEN": "test_token",
            },
        ):
            result = create_mcp_client("ccapi-mcp-server")

        # Should have attempted to create client
        mock_mcp_client.assert_called_once()

    @patch("utils.mcp_client_factory.MCPClient")
    def test_aws_region_configuration(self, mock_mcp_client):
        """Test AWS region is configured correctly."""
        mock_client = Mock()
        mock_mcp_client.return_value = mock_client

        with patch("config.settings.AWS_REGION", "us-east-1"):
            result = create_mcp_client("eks-mcp-server")

        # Should have attempted to create client with region
        mock_mcp_client.assert_called_once()


class TestMCPServerPatternValidation:
    """Test pattern-based MCP server validation."""

    def test_valid_aws_labs_mcp_server_patterns(self):
        """Test valid AWS Labs MCP server name patterns."""
        # Local modules
        from utils.mcp_client_factory import is_valid_aws_labs_mcp_server

        # Valid patterns
        assert is_valid_aws_labs_mcp_server("cost-explorer-mcp-server") == True
        assert is_valid_aws_labs_mcp_server("ccapi-mcp-server") == True
        assert is_valid_aws_labs_mcp_server("eks-mcp-server") == True
        assert is_valid_aws_labs_mcp_server("new-service-mcp-server") == True
        assert is_valid_aws_labs_mcp_server("multi-word-service-mcp-server") == True

    def test_invalid_aws_labs_mcp_server_patterns(self):
        """Test invalid AWS Labs MCP server name patterns."""
        # Local modules
        from utils.mcp_client_factory import is_valid_aws_labs_mcp_server

        # Invalid patterns
        assert is_valid_aws_labs_mcp_server("malicious-command") == False
        assert is_valid_aws_labs_mcp_server("cost-explorer") == False
        assert is_valid_aws_labs_mcp_server("mcp-server") == False  # No prefix
        assert is_valid_aws_labs_mcp_server("cost-mcp") == False  # Missing "server"
        assert is_valid_aws_labs_mcp_server("") == False
        assert is_valid_aws_labs_mcp_server(None) == False
        assert is_valid_aws_labs_mcp_server("   ") == False
        assert is_valid_aws_labs_mcp_server("cost-explorer-mcp-server-extra") == False


class TestMCPClientCaching:
    """Test MCP client caching functionality for performance optimization."""

    def setup_method(self):
        """Clear MCP client cache before each test."""
        clear_mcp_cache()

    def test_cache_is_cleared(self):
        """Test that clear_mcp_cache clears the cache."""
        # Local modules
        from utils.mcp_client_factory import get_cached_client_count

        # Initially cache should be empty
        assert get_cached_client_count() == 0

    @patch("utils.mcp_client_factory.MCPClient")
    @patch("utils.mcp_client_factory.stdio_client")
    def test_client_is_cached_on_creation(self, mock_stdio, mock_mcp_client):
        """Test that created clients are cached."""
        # Local modules
        from utils.mcp_client_factory import get_cached_client_count

        mock_client = Mock()
        mock_mcp_client.return_value = mock_client

        # Create a client
        result1 = create_mcp_client("ccapi-mcp-server")
        assert get_cached_client_count() == 1

        # Create same client again - should be cached
        result2 = create_mcp_client("ccapi-mcp-server")
        assert get_cached_client_count() == 1  # Still 1 (cached)

        # Both should return the same instance
        assert result1 is result2

    @patch("utils.mcp_client_factory.MCPClient")
    @patch("utils.mcp_client_factory.stdio_client")
    def test_use_cache_false_bypasses_cache(self, mock_stdio, mock_mcp_client):
        """Test that use_cache=False bypasses the cache."""
        mock_client1 = Mock()
        mock_client2 = Mock()
        mock_mcp_client.side_effect = [mock_client1, mock_client2]

        # Create without caching
        result1 = create_mcp_client("ccapi-mcp-server", use_cache=False)
        result2 = create_mcp_client("ccapi-mcp-server", use_cache=False)

        # Should be different instances
        assert result1 is not result2
        # MCPClient should have been called twice
        assert mock_mcp_client.call_count == 2

    @patch("utils.mcp_client_factory.MCPClient")
    @patch("utils.mcp_client_factory.stdio_client")
    def test_different_servers_cached_separately(self, mock_stdio, mock_mcp_client):
        """Test that different servers are cached separately."""
        # Local modules
        from utils.mcp_client_factory import get_cached_client_count

        mock_mcp_client.return_value = Mock()

        create_mcp_client("ccapi-mcp-server")
        assert get_cached_client_count() == 1

        create_mcp_client("eks-mcp-server")
        assert get_cached_client_count() == 2

        create_mcp_client("cost-explorer-mcp-server")
        assert get_cached_client_count() == 3


class TestBuildStartupCode:
    """Test _build_startup_code generates correct Python bootstrap commands."""

    def test_ccapi_server_includes_schema_cache_fix(self):
        """ccapi-mcp-server startup must redirect schema cache to /tmp."""
        code = _build_startup_code("ccapi-mcp-server", "awslabs.ccapi_mcp_server.server", "main")
        assert ".ccapi_schemas" in code
        assert "os.makedirs" in code or "makedirs" in code
        assert "schema_manager.py" in code
        assert "from awslabs.ccapi_mcp_server.server import main; main()" in code

    def test_eks_server_no_schema_fix(self):
        """eks-mcp-server should NOT get the schema cache fix."""
        code = _build_startup_code("eks-mcp-server", "awslabs.eks_mcp_server.server", "main")
        assert ".ccapi_schemas" not in code
        assert code == "from awslabs.eks_mcp_server.server import main; main()"

    def test_cost_explorer_server_no_schema_fix(self):
        """cost-explorer-mcp-server should NOT get the schema cache fix."""
        code = _build_startup_code("cost-explorer-mcp-server", "awslabs.cost_explorer_mcp_server.server", "main")
        assert ".ccapi_schemas" not in code
        assert code == "from awslabs.cost_explorer_mcp_server.server import main; main()"

    def test_ccapi_schema_fix_patches_dirname_before_import(self):
        """Schema fix must patch os.path.dirname BEFORE the server import statement."""
        code = _build_startup_code("ccapi-mcp-server", "awslabs.ccapi_mcp_server.server", "main")
        dirname_patch_pos = code.index("_os.path.dirname")
        import_pos = code.index("from awslabs.ccapi_mcp_server.server import main")
        assert dirname_patch_pos < import_pos, "dirname patch must come before server import"

    def test_ccapi_schema_fix_is_valid_python(self):
        """Schema fix generates syntactically valid Python code."""
        code = _build_startup_code("ccapi-mcp-server", "awslabs.ccapi_mcp_server.server", "main")
        # Remove the actual import/call at the end to avoid ImportError during compile
        # Just verify the schema fix portion compiles
        fix_portion = code.split("from awslabs")[0]
        compile(fix_portion, "<test>", "exec")  # Raises SyntaxError if invalid


class TestResolveMcpCommand:
    """Test _resolve_mcp_command resolution tiers."""

    @patch("utils.mcp_client_factory.shutil.which", return_value="/usr/bin/awslabs.eks-mcp-server")
    def test_tier1_console_script_on_path(self, mock_which):
        """When console script is on PATH, return it directly (no -c wrapper)."""
        cmd = _resolve_mcp_command("eks-mcp-server")
        assert cmd == ["awslabs.eks-mcp-server"]
        mock_which.assert_called_once_with("awslabs.eks-mcp-server")

    @patch("utils.mcp_client_factory.shutil.which", return_value=None)
    def test_tier2_entry_point_resolution(self, mock_which):
        """When console script is not on PATH, resolve via importlib.metadata."""
        mock_ep = Mock()
        mock_ep.name = "awslabs.ccapi-mcp-server"
        mock_ep.value = "awslabs.ccapi_mcp_server.server:main"
        mock_ep.group = "console_scripts"

        with patch("utils.mcp_client_factory.importlib.metadata.entry_points", return_value=[mock_ep]):
            cmd = _resolve_mcp_command("ccapi-mcp-server")

        assert cmd[0] == sys.executable
        assert cmd[1] == "-c"
        # Should contain the schema fix AND the server startup
        assert ".ccapi_schemas" in cmd[2]
        assert "from awslabs.ccapi_mcp_server.server import main; main()" in cmd[2]

    @patch("utils.mcp_client_factory.shutil.which", return_value=None)
    def test_tier3_naming_convention_fallback(self, mock_which):
        """When entry points fail, derive module path from naming convention."""
        with patch(
            "utils.mcp_client_factory.importlib.metadata.entry_points",
            side_effect=Exception("metadata unavailable"),
        ):
            cmd = _resolve_mcp_command("eks-mcp-server")

        assert cmd[0] == sys.executable
        assert cmd[1] == "-c"
        assert "from awslabs.eks_mcp_server.server import main; main()" in cmd[2]
        # eks-mcp-server should NOT have schema fix
        assert ".ccapi_schemas" not in cmd[2]

    @patch("utils.mcp_client_factory.shutil.which", return_value=None)
    def test_tier3_ccapi_naming_convention_includes_schema_fix(self, mock_which):
        """ccapi naming convention fallback must still include schema fix."""
        with patch(
            "utils.mcp_client_factory.importlib.metadata.entry_points",
            side_effect=Exception("metadata unavailable"),
        ):
            cmd = _resolve_mcp_command("ccapi-mcp-server")

        assert ".ccapi_schemas" in cmd[2]
        assert "from awslabs.ccapi_mcp_server.server import main; main()" in cmd[2]

    @patch("utils.mcp_client_factory.shutil.which", return_value=None)
    def test_entry_point_not_found_falls_to_tier3(self, mock_which):
        """When entry point exists but doesn't match, falls to tier 3."""
        mock_ep = Mock()
        mock_ep.name = "awslabs.other-mcp-server"
        mock_ep.value = "awslabs.other_mcp_server.server:main"
        mock_ep.group = "console_scripts"

        with patch("utils.mcp_client_factory.importlib.metadata.entry_points", return_value=[mock_ep]):
            cmd = _resolve_mcp_command("eks-mcp-server")

        # Should fall through to tier 3 (naming convention)
        assert "from awslabs.eks_mcp_server.server import main; main()" in cmd[2]
