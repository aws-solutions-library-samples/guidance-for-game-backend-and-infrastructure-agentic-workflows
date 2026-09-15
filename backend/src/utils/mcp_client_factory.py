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

# MCPTransport is re-exported from the package root (strands.tools.mcp.__all__),
# so import it there rather than the internal mcp_types implementation module to
# avoid a runtime dependency on the package's internal layout.
from strands.tools.mcp import MCPClient, MCPTransport

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

    Most servers just import+call their entry point; read-only-filesystem needs
    are handled via env vars in create_mcp_client. The billing-cost-management
    server is the exception: it writes a SQLite session DB to a path hardcoded
    relative to its own package dir (utilities/sql_utils.py:get_session_db_path),
    with NO env/CLI override, so on the read-only AgentCore container it crashes
    on first SQL-tool use. We pin its session-DB path into a writable /tmp dir
    (from GBAW_BILLING_MCP_DB_DIR, set in create_mcp_client) by pre-seeding the
    module's _SESSION_DB_PATH global before main() runs. Guarded so an upstream
    refactor degrades gracefully rather than hard-failing startup.
    """
    if "billing-cost-management" in server_name:
        return (
            "import os, uuid\n"
            "try:\n"
            "    from awslabs.billing_cost_management_mcp_server.utilities import sql_utils as _sql\n"
            "    _dir = os.environ.get('GBAW_BILLING_MCP_DB_DIR', os.path.join(os.environ.get('TMPDIR', '/tmp'), 'billing-cost-mcp'))\n"
            "    os.makedirs(_dir, exist_ok=True)\n"
            "    _sql._SESSION_DB_PATH = os.path.join(_dir, f'session_{uuid.uuid4().hex[:8]}.db')\n"
            "except Exception as _e:\n"
            "    import sys as _sys; print(f'billing-mcp db-path patch skipped: {_e}', file=_sys.stderr)\n"
            f"from {module} import {func}; {func}()"
        )
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
        # Python 3.9-3.11: entry_points() returns a dict keyed by group; Python
        # 3.12+ returns an EntryPoints collection that is iterable but has no
        # .get(). Branch on the concrete type with an if/else (not a ternary) so
        # the .get() call is scoped to the narrowed dict type and type-checks
        # cleanly under both mypy versions we run.
        if isinstance(eps, dict):
            console_scripts = eps.get("console_scripts", [])
        else:
            console_scripts = [ep for ep in eps if ep.group == "console_scripts"]
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
                # Redirecting HOME relocates the server's $HOME/.aws/aws-api-mcp
                # log off the read-only container FS. But it also hides
                # ~/.aws/{config,credentials} from the subprocess. Deployed
                # AgentCore uses container-role env creds (unaffected), but local
                # AWS_PROFILE-based dev would break — so pin the AWS cred/config
                # files to the ORIGINAL home (only if not already set explicitly).
                original_home = os.environ.get("HOME")
                if original_home:
                    env.setdefault("AWS_CONFIG_FILE", os.path.join(original_home, ".aws", "config"))
                    env.setdefault("AWS_SHARED_CREDENTIALS_FILE", os.path.join(original_home, ".aws", "credentials"))
                env["HOME"] = aws_api_home
                env["AWS_API_MCP_WORKING_DIR"] = aws_api_workdir
                # Read-only: the EKS specialist only needs discovery (eks list/
                # describe). NOTE: aws-api-mcp-server classifies read vs write via a
                # service-reference lookup; if that lookup can't be reached in a
                # restricted-egress runtime it falls back to rejecting all calls.
                env["READ_OPERATIONS_ONLY"] = "true"

            # billing-cost-management-mcp-server writes to its own package dir in
            # TWO places that fail on the read-only container:
            #   1) a log file under <pkg>/logs at IMPORT time (logging_utils) — but
            #      it honors FASTMCP_LOG_FILE, so point that at /tmp to skip the
            #      package-dir makedirs entirely.
            #   2) a SQLite session DB under <pkg>/sessions (sql_utils) with no env
            #      override — relocated by the _build_startup_code patch, which
            #      reads GBAW_BILLING_MCP_DB_DIR (set here).
            # FASTMCP_LOG_FILE must be set or the import-time log makedirs crashes
            # BEFORE the DB patch can run.
            if "billing-cost-management" in server_name:
                billing_base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "billing-cost-mcp")
                billing_db_dir = os.path.join(billing_base, "sessions")
                billing_log_dir = os.path.join(billing_base, "logs")
                os.makedirs(billing_db_dir, exist_ok=True)
                os.makedirs(billing_log_dir, exist_ok=True)
                env["GBAW_BILLING_MCP_DB_DIR"] = billing_db_dir
                env["FASTMCP_LOG_FILE"] = os.path.join(billing_log_dir, "billing-cost-management-mcp-server.log")

            # Use wrapper to filter non-JSON stdout (AWS Labs MCP servers print diagnostics)
            wrapper_path = os.path.join(os.path.dirname(__file__), "mcp_wrapper.py")

            # MCPClient wants a zero-arg transport factory (Callable[[], MCPTransport]).
            # A lambda with snapshot default args can't be matched to that signature by
            # mypy ("Cannot infer type of lambda"); a plain closure has the right arity.
            # Closing over the loop locals is safe here — we return immediately below,
            # so they can't be rebound before the factory is first called.
            def _transport() -> MCPTransport:
                return stdio_client(
                    StdioServerParameters(command=sys.executable, args=[wrapper_path] + mcp_cmd, env=env)
                )

            client = MCPClient(_transport)

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
