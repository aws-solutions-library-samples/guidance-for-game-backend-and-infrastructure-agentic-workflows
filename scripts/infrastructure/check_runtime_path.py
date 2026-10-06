#!/usr/bin/env python3
"""Preflight guard against an unbootable long-path AgentCore runtime (issue #517).

`agentcore launch` packages the backend venv's console scripts (including the
``opentelemetry-instrument`` entrypoint) into
``backend/.bedrock_agentcore/gameagentruntime/dependencies.zip``. uv generates
each console script's first line with the same rule as pip/distlib: on POSIX it
embeds a ``#!<interpreter>`` shebang, but when that shebang would be *non-simple*
it falls back to a ``/bin/sh`` exec trampoline that embeds the absolute LOCAL
interpreter path. The starter toolkit's shebang rewriter only normalizes
single-line ``#!`` shebangs, so the trampoline ships unchanged and the runtime
dies at exec inside the read-only container::

    /var/task/bin/opentelemetry-instrument: line 2:
      /Users/.../backend/.venv/bin/python3: No such file or directory

The failure is silent: deploy exits 0 and the control-plane status reaches
READY, so validate-deployment.sh passes while every invocation fails.

uv's exact predicate (uv 0.12.0, ``crates/uv-install-wheel/src/wheel.rs``,
``format_shebang``)::

    let executable = executable.simplified_display().to_string();
    if os_name == "posix" {
        let shebang_length = 2 + executable.len() + 1;   // "#!" + path + "\n"
        if shebang_length > 127 || executable.contains(' ') || relocatable {
            // ...emit the /bin/sh trampoline...
        }
    }
    // ...otherwise "#!{executable}"

Three facts drive this guard and were the root causes of the first rejected fix:

1. The threshold is on ``executable.len()`` which is the **byte length** of the
   path (Rust ``String::len`` counts UTF-8 bytes). ``2 + len + 1 > 127`` is
   ``len > 124``: the trampoline fires when the interpreter path is **over 124
   filesystem bytes**, or when it **contains a space** -- not when the whole
   line is "over 127 characters".
2. The byte/space check is gated on ``os_name == "posix"``. On native Windows
   uv wraps the script in a native launcher binary
   (``uv_trampoline_builder::windows_script_launcher``) that natively supports
   long and relocatable paths, so a POSIX shebang predicate must **not** reject
   a Windows interpreter path.
3. The path that matters is the **actual venv interpreter** that uv wrote the
   scripts against (``layout.sys_executable``), so we resolve the real venv
   interpreter rather than assuming a fixed string.

This module is both an importable predicate (for tests) and a CLI the shell and
PowerShell deploy paths invoke right after local ``uv sync`` and before the
first AWS-mutating command. Standard library only: the plain-shell deploy path
runs it with a bare ``python3`` before the venv is guaranteed usable.
"""

from __future__ import annotations

# Standard library
import argparse
import os
import sys
from pathlib import Path

# uv's shebang-length ceiling (the full `#!<path>\n` line). Mirrors the literal
# `127` in uv's `format_shebang`. The interpreter path itself is unbootable at
# `len > SHEBANG_LINE_LIMIT - 3` bytes (the `#`, `!`, and trailing newline).
SHEBANG_LINE_LIMIT = 127
SHEBANG_FIXED_OVERHEAD = 3  # "#!" + "\n"
# Over-124-byte interpreter paths trip the trampoline.
INTERPRETER_BYTE_LIMIT = SHEBANG_LINE_LIMIT - SHEBANG_FIXED_OVERHEAD


def interpreter_path_bytes(interpreter_path: str) -> int:
    """Filesystem byte length of ``interpreter_path``.

    uv measures ``String::len`` (UTF-8 bytes), not Unicode code points. A
    multibyte path (accented or CJK directory names) can be far longer in bytes
    than in characters, so a char count would under-measure and pass a path uv
    would actually reject. ``os.fsencode`` reproduces the on-disk byte length
    uv sees.
    """
    return len(os.fsencode(interpreter_path))


def is_windows_launcher(interpreter_path: str, *, os_name: str | None = None) -> bool:
    """True when the interpreter is a native Windows launcher target.

    On Windows uv emits a native launcher binary that natively supports long and
    relocatable interpreter paths -- the POSIX shebang trampoline never applies.
    Detection keys off the running OS first (authoritative when the deploy runs
    on Windows) and falls back to a ``.exe`` suffix so a caller can probe a
    Windows path from a POSIX test host.
    """
    resolved_os_name = os_name if os_name is not None else os.name
    if resolved_os_name == "nt":
        return True
    return interpreter_path.lower().endswith(".exe")


def venv_python_is_bootable(interpreter_path: str, *, os_name: str | None = None) -> bool:
    """Return True when uv would emit a portable ``#!`` shebang for this path.

    Returns False only for the POSIX case uv turns into the unbootable
    ``/bin/sh`` trampoline: an interpreter path over 124 filesystem bytes, or
    one containing a space. Native Windows launcher targets are always bootable.
    """
    if is_windows_launcher(interpreter_path, os_name=os_name):
        return True
    if " " in interpreter_path:
        return False
    return interpreter_path_bytes(interpreter_path) <= INTERPRETER_BYTE_LIMIT


def resolve_venv_interpreter(backend_dir: Path) -> str:
    """Resolve the interpreter path uv wrote the venv's console scripts against.

    Prefer the venv's own recorded interpreter so the guard measures exactly
    what ``agentcore launch`` will package:

    * ``pyvenv.cfg``'s ``home`` is informational only; the launched scripts use
      the venv's own ``bin/python3`` (POSIX) / ``Scripts/python.exe`` (Windows),
      whose path is what uv embeds. We return that concrete interpreter path.
    * If the venv is absent (guard invoked before ``uv sync``), fall back to the
      deterministic path uv would create, so the length/space check is still
      meaningful.

    The path is returned absolute and unresolved: uv embeds the path as invoked
    (``layout.sys_executable``), and symlink resolution could mask the real
    on-disk length that ships in the trampoline.
    """
    venv_dir = backend_dir / ".venv"
    if os.name == "nt":
        interpreter = venv_dir / "Scripts" / "python.exe"
    else:
        interpreter = venv_dir / "bin" / "python3"
    return os.path.abspath(interpreter)


def remediation_message(interpreter_path: str) -> str:
    """Actionable failure text naming the real limit and a concrete fix."""
    byte_length = interpreter_path_bytes(interpreter_path)
    has_space = " " in interpreter_path
    if has_space:
        cause = "contains a space"
    else:
        cause = f"is {byte_length} filesystem bytes (over the {INTERPRETER_BYTE_LIMIT}-byte limit)"
    return (
        f"Backend venv interpreter path {cause}.\n"
        f"   Path: {interpreter_path}\n"
        "\n"
        "   uv writes the venv's console scripts with a /bin/sh trampoline that\n"
        "   embeds this absolute local path instead of a portable shebang when the\n"
        f"   interpreter path exceeds {INTERPRETER_BYTE_LIMIT} bytes or contains a space. agentcore\n"
        "   launch packages those scripts into dependencies.zip, producing an\n"
        "   AgentCore runtime that cannot start (issue #517).\n"
        "\n"
        "   Deploy from a checkout whose path is shorter and space-free so that\n"
        f"   backend/.venv/bin/python3 is at most {INTERPRETER_BYTE_LIMIT} bytes, then re-run the\n"
        "   deployment."
    )


def _default_backend_dir() -> Path:
    # scripts/infrastructure/check_runtime_path.py -> repository root is parents[2]
    return Path(__file__).resolve().parents[2] / "backend"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail fast when the backend venv interpreter path would ship an "
            "unbootable AgentCore runtime (issue #517)."
        ),
    )
    parser.add_argument(
        "--backend-dir",
        type=Path,
        default=_default_backend_dir(),
        help="Backend package directory containing the .venv (default: repository backend/).",
    )
    parser.add_argument(
        "--interpreter",
        default=None,
        help="Interpreter path to check directly, bypassing venv resolution (for testing).",
    )
    args = parser.parse_args(argv)

    interpreter_path = args.interpreter or resolve_venv_interpreter(args.backend_dir)

    if venv_python_is_bootable(interpreter_path):
        return 0

    sys.stderr.write("\u274c " + remediation_message(interpreter_path) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
