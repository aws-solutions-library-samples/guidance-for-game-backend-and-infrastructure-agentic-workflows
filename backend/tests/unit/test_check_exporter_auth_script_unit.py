#!/usr/bin/env python3
"""Shell-level fail-closed tests for scripts/infrastructure/check-exporter-auth.sh (#420).

The confirmed fail-open bug: when the CloudWatch Logs query failed, the wrapper
masked the error (``|| echo "[]"``) so the classifier saw zero events and
reported a clean PASS (exit 0). These tests drive the real script with a stubbed
``aws`` on PATH to prove:

  * A failing log query (e.g. a nonexistent profile) now exits 2 (unavailable ->
    WARN), NOT 0 (clean PASS).
  * The public-safe summary says the check could not run / status is UNKNOWN and
    leaks no stderr, ARNs, accounts, or endpoints.
  * A successful query with a genuine #420 signature still classifies (it does
    not become "unavailable"), proving the fail-closed guard is scoped to query
    failure and did not break the happy path.

The script shells out to ``uv run python`` to invoke the pure detector, so these
run only where ``uv`` is available; they skip otherwise.
"""

# Standard library
import json
import os
import pathlib
import shutil
import subprocess

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit]

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "scripts" / "infrastructure" / "check-exporter-auth.sh"

_NEEDS = ("bash", "uv")
_MISSING = [t for t in _NEEDS if shutil.which(t) is None]
skip_if_missing = pytest.mark.skipif(bool(_MISSING), reason=f"missing tools: {_MISSING}")


def _write_exec(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), "--runtime-id", "test-runtime", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _base_env(bin_dir: pathlib.Path) -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AWS_REGION"] = "us-west-2"
    # Ensure the script's `date`/`mktemp`/`python3` still resolve from real PATH.
    return env


def test_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@skip_if_missing
def test_nonexistent_profile_query_failure_is_unavailable_warn_not_pass(tmp_path):
    """A failed log query (bad profile) must exit 2 (unavailable), never 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Stub aws: emulate the CLI's "profile could not be found" failure on stderr.
    _write_exec(
        bin_dir / "aws",
        "#!/bin/sh\n"
        'if [ "$1" = "configure" ]; then echo us-west-2; exit 0; fi\n'
        '>&2 echo "The config profile (ghost) could not be found"\n'
        "exit 255\n",
    )
    env = _base_env(bin_dir)
    result = _run(env, "--lookback-hours", "24")

    assert result.returncode == 2, f"expected unavailable exit 2, got {result.returncode}: {result.stdout}"
    out = result.stdout
    assert "WARN" in out
    assert ("UNKNOWN" in out) or ("could not run" in out)
    # Public-safe: the raw stderr / profile name must not leak into stdout.
    assert "ghost" not in out
    assert "arn:aws" not in out
    # It must NOT be reported as clean/OK.
    assert "OK — no exporter" not in out


@skip_if_missing
def test_access_denied_query_failure_is_unavailable(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(
        bin_dir / "aws",
        "#!/bin/sh\n"
        'if [ "$1" = "configure" ]; then echo us-west-2; exit 0; fi\n'
        '>&2 echo "AccessDeniedException: not authorized to perform logs:FilterLogEvents"\n'
        "exit 254\n",
    )
    result = _run(_base_env(bin_dir), "--lookback-hours", "24")
    assert result.returncode == 2
    assert "WARN" in result.stdout
    assert "not authorized" not in result.stdout  # no stderr leak


@skip_if_missing
def test_successful_clean_query_is_pass(tmp_path):
    """A successful query returning no matching events is a clean PASS (exit 0)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(
        bin_dir / "aws",
        "#!/bin/sh\n" 'if [ "$1" = "configure" ]; then echo us-west-2; exit 0; fi\n' "echo '[]'\n" "exit 0\n",
    )
    result = _run(_base_env(bin_dir), "--lookback-hours", "24")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@skip_if_missing
def test_successful_query_with_403_signature_classifies_not_unavailable(tmp_path):
    """A real #420 403 signature must classify (persistent here), not 'unavailable'.

    Proves the fail-closed guard is scoped to query *failure* and did not swallow
    the happy path. The single 403 has no adjacent transition -> persistent FAIL.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    events = json.dumps([{"timestamp": 500000, "body": "Failed to export span batch code: 403, reason: Forbidden"}])
    # First filter-log-events call (exporter stream) returns the 403; the
    # transition-window call(s) return empty. Distinguish by the presence of the
    # --log-stream-names flag used only in the first query.
    _write_exec(
        bin_dir / "aws",
        "#!/bin/sh\n"
        'if [ "$1" = "configure" ]; then echo us-west-2; exit 0; fi\n'
        'for a in "$@"; do if [ "$a" = "--log-stream-names" ]; then '
        f"printf '%s' '{events}'; exit 0; fi; done\n"
        "echo '[]'\n"
        "exit 0\n",
    )
    result = _run(_base_env(bin_dir), "--lookback-hours", "24")
    # Not unavailable (exit 2); a real persistent signature -> FAIL (exit 1).
    assert result.returncode == 1, f"got {result.returncode}: {result.stdout}"
    assert "FAIL" in result.stdout
    assert "UNKNOWN" not in result.stdout
