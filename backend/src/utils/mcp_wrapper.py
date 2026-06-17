#!/usr/bin/env python3
"""
MCP Server Wrapper - Redirects non-JSON stdout to stderr.

AWS Labs MCP servers print diagnostic messages to stdout on startup,
which breaks the JSON-RPC protocol. This wrapper filters stdout to
only pass valid JSON-RPC messages, redirecting everything else to stderr.
"""

# Standard library
import json
import subprocess
import sys
import threading


def is_jsonrpc(line: str) -> bool:
    """Check if a line is valid JSON-RPC."""
    if not line.strip():
        return False
    try:
        data = json.loads(line)
        # JSON-RPC messages have jsonrpc field or are responses
        return isinstance(data, dict) and ("jsonrpc" in data or "result" in data or "error" in data)
    except (json.JSONDecodeError, TypeError):
        return False


def filter_stdout(proc_stdout, real_stdout):
    """Filter stdout, passing only JSON-RPC to real stdout, rest to stderr."""
    for line in proc_stdout:
        if is_jsonrpc(line):
            real_stdout.write(line)
            real_stdout.flush()
        else:
            # Redirect non-JSON to stderr
            sys.stderr.write(f"[MCP-INFO] {line}")
            sys.stderr.flush()


def main():
    if len(sys.argv) < 2:
        print("Usage: mcp_wrapper.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1:]

    proc = subprocess.Popen(
        cmd, stdin=sys.stdin, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1  # Line buffered
    )

    # Filter stdout in a thread
    stdout_thread = threading.Thread(target=filter_stdout, args=(proc.stdout, sys.stdout), daemon=True)
    stdout_thread.start()

    # Wait for process
    proc.wait()
    stdout_thread.join(timeout=1)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
