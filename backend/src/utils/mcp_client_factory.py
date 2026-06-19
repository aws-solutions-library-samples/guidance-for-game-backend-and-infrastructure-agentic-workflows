"""
MCP Client Factory - Uses pre-installed AWS Labs MCP servers

Strands handles MCP client connection lifecycles via stdio transport.
AWS Labs MCP servers are pre-installed Python packages for maximum performance.

Note: AWS Labs MCP servers print diagnostic messages to stdout on startup,
which breaks JSON-RPC. We use a wrapper to filter non-JSON output to stderr.

Performance Optimization:
- MCP clients are cached at module level to avoid recreation overhead
- Cache is reused across specialist agent calls within the same process
- Reduces multi-agent query latency by ~100-400ms per cached client
"""

# Standard library
import importlib.metadata
import os
import shutil
import sys
import threading
import time
from typing import Dict, List, Optional

# Third-party packages
from mcp import StdioServerParameters, stdio_client
from strands.tools.mcp import MCPClient

# Local modules
from config.settings import AWS_REGION, RETRY_MAX_ATTEMPTS
from utils.logger import logger

# MCP client creation retries (fewer than general RETRY_MAX_ATTEMPTS since
# each attempt involves spawning a subprocess via stdio).
MCP_CREATE_MAX_ATTEMPTS = min(RETRY_MAX_ATTEMPTS, 2)
MCP_CREATE_RETRY_DELAY = 1.0  # seconds between attempts

# Module-level MCP client cache for performance
# Thread-safe cache to reuse MCP clients across specialist calls
_mcp_client_cache: Dict[str, MCPClient] = {}
_cache_lock = threading.Lock()


def is_valid_aws_labs_mcp_server(server_name: str) -> bool:
    """Validate AWS Labs MCP server name pattern for security."""
    if not server_name or not isinstance(server_name, str):
        return False

    server_name = server_name.strip()
    if not server_name.endswith("-mcp-server"):
        return False

    parts = server_name.split("-")
    if len(parts) < 3 or parts[-2] != "mcp" or parts[-1] != "server":
        return False

    prefix_parts = parts[:-2]
    for part in prefix_parts:
        if not part or not part.replace("-", "").isalnum():
            return False

    return True


def _build_startup_code(server_name: str, module: str, func: str) -> str:
    """
    Build Python startup code for an MCP server entry point.

    (Read-only-filesystem workarounds for individual servers are handled via
    environment variables in create_mcp_client, not via import-time patches.)
    """
    return f"from {module} import {func}; {func}()"


def _resolve_mcp_command(server_name: str) -> List[str]:
    """
    Resolve MCP server to a runnable command.

    Tries console script on PATH first (works in Docker/venv), then falls back
    to entry point resolution via importlib.metadata (works in AgentCore
    direct_code_deploy where console scripts aren't on PATH).
    """
    executable_name = f"awslabs.{server_name}"

    # Try console script on PATH first (Docker, venv, standard pip install)
    if shutil.which(executable_name):
        logger.debug(f"📍 {server_name}: using console script on PATH")
        return [executable_name]

    # Resolve via importlib.metadata entry points (AgentCore direct_code_deploy)
    try:
        eps = importlib.metadata.entry_points()
        # Python 3.9-3.11: eps is dict-like; Python 3.12+: SelectableGroups
        console_scripts = (
            eps.get("console_scripts", [])
            if isinstance(eps, dict)
            else [ep for ep in eps if ep.group == "console_scripts"]
        )
        for ep in console_scripts:
            if ep.name == executable_name:
                module, func = ep.value.rsplit(":", 1)
                logger.debug(f"📍 {server_name}: using entry point {module}:{func}")
                return [sys.executable, "-c", _build_startup_code(server_name, module, func)]
    except Exception as e:
        logger.debug(f"📍 {server_name}: entry point lookup failed: {e}")

    # Last resort: derive module path from AWS Labs naming convention
    # e.g., aws-api-mcp-server -> awslabs.aws_api_mcp_server.server:main
    module_name = f"awslabs.{server_name.replace('-', '_')}.server"
    logger.debug(f"📍 {server_name}: using naming convention {module_name}:main")
    return [sys.executable, "-c", _build_startup_code(server_name, module_name, "main")]


def create_mcp_client(server_name: str, use_cache: bool = True) -> Optional[MCPClient]:
    """
    Create or retrieve cached MCP client using pre-installed AWS Labs MCP servers.

    Args:
        server_name: MCP server name (e.g., "aws-api-mcp-server")
        use_cache: If True, return cached client if available (default: True)

    Returns:
        MCPClient instance or None if failed
    """
    if not is_valid_aws_labs_mcp_server(server_name):
        logger.warning(f"❌ Invalid server name: {server_name}")
        return None

    # Check cache first (thread-safe)
    if use_cache:
        with _cache_lock:
            if server_name in _mcp_client_cache:
                logger.debug(f"♻️ Reusing cached {server_name} MCP client")
                return _mcp_client_cache[server_name]

    # Retry loop for transient stdio startup failures (Well-Architected: Reliability 5.2)
    last_error = None
    for attempt in range(1, MCP_CREATE_MAX_ATTEMPTS + 1):
        try:
            logger.debug(f"🚀 Creating {server_name} MCP client (attempt {attempt}/{MCP_CREATE_MAX_ATTEMPTS})")

            # Resolve the MCP server command (console script or entry point fallback)
            mcp_cmd = _resolve_mcp_command(server_name)

            # Build environment - inherit parent env for IAM role credentials
            env = dict(os.environ)
            env["AWS_REGION"] = AWS_REGION
            env["AWS_DEFAULT_REGION"] = AWS_REGION

            # aws-api-mcp-server writes a log under $HOME/.aws/aws-api-mcp and
            # requires an existing working dir. On the read-only AgentCore
            # container both would fail, so redirect them to writable /tmp. There
            # is no env to relocate the log other than HOME. READ_OPERATIONS_ONLY
            # caps it to read calls (the EKS specialist only does discovery, e.g.
            # `aws eks list-clusters`). Both dirs MUST exist before launch.
            if "aws-api" in server_name:
                tmp_base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "aws-api-mcp")
                aws_api_home = os.path.join(tmp_base, "home")
                aws_api_workdir = os.path.join(tmp_base, "workdir")
                os.makedirs(aws_api_home, exist_ok=True)
                os.makedirs(aws_api_workdir, exist_ok=True)
                env["HOME"] = aws_api_home
                env["AWS_API_MCP_WORKING_DIR"] = aws_api_workdir
                env["READ_OPERATIONS_ONLY"] = "true"

            # Use wrapper to filter non-JSON stdout (AWS Labs MCP servers print diagnostics)
            wrapper_path = os.path.join(os.path.dirname(__file__), "mcp_wrapper.py")

            client = MCPClient(
                lambda cmd=mcp_cmd, e=env, wp=wrapper_path: stdio_client(
                    StdioServerParameters(command=sys.executable, args=[wp] + cmd, env=e)
                )
            )

            # Cache the client (thread-safe)
            if use_cache:
                with _cache_lock:
                    _mcp_client_cache[server_name] = client
                    logger.debug(f"💾 Cached {server_name} MCP client")

            logger.debug(f"✅ {server_name} MCP client created")
            return client

        except Exception as e:
            last_error = e
            if attempt < MCP_CREATE_MAX_ATTEMPTS:
                logger.warning(
                    f"⚠️ {server_name} MCP client attempt {attempt}/{MCP_CREATE_MAX_ATTEMPTS} "
                    f"failed: {e}, retrying in {MCP_CREATE_RETRY_DELAY}s"
                )
                time.sleep(MCP_CREATE_RETRY_DELAY)
            else:
                logger.error(
                    f"❌ Failed to create {server_name} MCP client after {MCP_CREATE_MAX_ATTEMPTS} attempts: {e}"
                )

    return None


def clear_mcp_cache() -> None:
    """Clear the MCP client cache. Useful for testing or cleanup."""
    with _cache_lock:
        _mcp_client_cache.clear()
        logger.debug("🗑️ MCP client cache cleared")


def get_cached_client_count() -> int:
    """Get the number of cached MCP clients. Useful for monitoring."""
    with _cache_lock:
        return len(_mcp_client_cache)
