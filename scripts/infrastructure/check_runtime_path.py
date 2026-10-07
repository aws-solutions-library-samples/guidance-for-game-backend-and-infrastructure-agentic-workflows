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
3. The path that matters is the **actual interpreter uv selected** for the
   project environment and wrote the console scripts against
   (``layout.sys_executable``). uv 0.12.0 lets ``UV_PROJECT_ENVIRONMENT``
   redirect that environment away from ``<backend>/.venv``, so a guard that
   reconstructs ``.venv/bin/python3`` from the backend directory can measure a
   *different* interpreter than the one that ships -- passing a short synthetic
   path while uv packages a long real one. Both deploy paths invoke this guard
   with ``uv run --project <backend>``, which runs it *inside* that selected
   environment, so the running ``sys.executable`` IS the interpreter uv embeds.
   The guard therefore measures ``sys.executable`` by default and treats the
   backend ``.venv`` reconstruction only as a fallback for a non-uv invocation.

This module is both an importable predicate (for tests) and a CLI the shell and
PowerShell deploy paths invoke right after local ``uv sync`` and before the
first AWS-mutating command. Standard library only.
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


def uses_native_windows_launcher(os_name: str | None = None) -> bool:
    """True when uv wraps console scripts in a native Windows launcher binary.

    uv's shebang rule is gated entirely on the target environment's OS
    (``format_shebang`` runs its length/space check only under
    ``os_name == "posix"``), and on Windows ``write_script_entrypoints`` wraps
    each launcher in a native ``.exe`` trampoline
    (``uv_trampoline_builder::windows_script_launcher``) that natively supports
    long and relocatable interpreter paths. So the decision is purely the OS uv
    runs under -- NOT a property of the interpreter path string.

    The ``.exe`` suffix is deliberately NOT consulted: on a POSIX host uv never
    emits a Windows launcher, so a long or space-bearing ``/.../python.exe`` path
    still gets the unbootable ``/bin/sh`` trampoline. Keying off the suffix would
    wrongly pass exactly that path (the first fix's root-cause defect).

    ``os_name`` defaults to the running ``os.name``: the deploy host that runs
    ``agentcore launch`` is the host whose uv packages the venv, so its OS is
    authoritative for whether the shipped scripts are launchers or shebangs.
    """
    resolved_os_name = os_name if os_name is not None else os.name
    return resolved_os_name == "nt"


def venv_python_is_bootable(interpreter_path: str, *, os_name: str | None = None) -> bool:
    """Return True when uv would emit a portable, bootable console script.

    On a native-Windows target uv ships a native launcher binary, so any
    interpreter path is bootable regardless of length or spaces. On POSIX,
    returns False exactly for the case uv turns into the unbootable ``/bin/sh``
    trampoline: an interpreter path over 124 filesystem bytes, or one containing
    a space.
    """
    if uses_native_windows_launcher(os_name):
        return True
    if " " in interpreter_path:
        return False
    return interpreter_path_bytes(interpreter_path) <= INTERPRETER_BYTE_LIMIT


def resolve_venv_interpreter(backend_dir: Path) -> str:
    """Reconstruct the deterministic ``.venv`` interpreter path under ``backend_dir``.

    This is a **fallback** for a non-uv invocation only. It assumes uv's default
    project environment layout (``.venv/bin/python3`` on POSIX,
    ``.venv/Scripts/python.exe`` on Windows). It must NOT be used when the guard
    runs inside the uv-selected environment, because ``UV_PROJECT_ENVIRONMENT``
    can point the real environment elsewhere; use :func:`resolve_runtime_interpreter`
    for the authoritative selection.

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


def _running_in_project_environment(backend_dir: Path) -> bool:
    """True when the running interpreter belongs to a uv-selected environment.

    ``uv run`` sets ``VIRTUAL_ENV`` to the environment it selected (honoring
    ``UV_PROJECT_ENVIRONMENT``), and ``sys.executable`` then lives inside it.
    We also accept the plain default ``<backend>/.venv`` layout so a direct
    ``<backend>/.venv/bin/python3 check_runtime_path.py`` invocation is trusted.
    A bare system ``python3`` (no venv) is not trusted to represent the runtime.

    Paths are compared with ``abspath`` (not ``resolve``): the venv's
    ``bin/python3`` is itself a symlink to the base interpreter, so resolving it
    would walk *out* of the environment and break the containment check. uv
    likewise embeds the unresolved ``sys.executable`` path.
    """
    executable = Path(os.path.abspath(sys.executable))
    candidates = []
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        candidates.append(Path(os.path.abspath(virtual_env)))
    candidates.append(Path(os.path.abspath(backend_dir / ".venv")))
    return any(candidate in executable.parents for candidate in candidates)


def resolve_runtime_interpreter(backend_dir: Path) -> str:
    """Resolve the interpreter uv wrote the venv's console scripts against.

    Prefer the running ``sys.executable`` when the guard is executing inside the
    uv-selected project environment: that is exactly the interpreter whose path
    uv embeds into the console scripts that ``agentcore launch`` packages,
    including any ``UV_PROJECT_ENVIRONMENT`` redirect. Only when the guard is
    *not* running inside that environment (a bare ``python3`` fallback before
    the venv is usable) do we reconstruct the deterministic ``.venv`` path, so
    the length/space check stays meaningful.

    Returned absolute and unresolved (see :func:`resolve_venv_interpreter`).
    """
    if _running_in_project_environment(backend_dir):
        return os.path.abspath(sys.executable)
    return resolve_venv_interpreter(backend_dir)


def _interpreter_is_external_override(interpreter_path: str, backend_dir: Path | None) -> bool:
    """True when ``interpreter_path`` lives outside ``<backend>/.venv``.

    When uv honors ``UV_PROJECT_ENVIRONMENT`` the selected interpreter can be an
    absolute path with no relationship to ``<backend>/.venv``. Shortening the
    checkout cannot repair such a path, so remediation must be tailored. When
    ``backend_dir`` is unknown (a direct ``--interpreter`` probe) we cannot make
    the determination and treat the path as the default-layout case.
    """
    if backend_dir is None:
        return False
    default_venv = Path(os.path.abspath(backend_dir / ".venv"))
    measured = Path(os.path.abspath(interpreter_path))
    return default_venv not in measured.parents and measured != default_venv


def remediation_message(interpreter_path: str, *, backend_dir: Path | None = None) -> str:
    """Actionable failure text naming the real limit and the right concrete fix.

    The fix differs by where the unbootable interpreter came from. A path under
    the default ``<backend>/.venv`` is repaired by deploying from a shorter,
    space-free checkout. An interpreter uv selected via ``UV_PROJECT_ENVIRONMENT``
    (outside ``<backend>/.venv``) cannot be fixed by moving the checkout; the
    override itself must be unset or pointed at a short, space-free directory.
    """
    byte_length = interpreter_path_bytes(interpreter_path)
    has_space = " " in interpreter_path
    if has_space:
        cause = "contains a space"
    else:
        cause = f"is {byte_length} filesystem bytes (over the {INTERPRETER_BYTE_LIMIT}-byte limit)"

    if _interpreter_is_external_override(interpreter_path, backend_dir):
        fix = (
            "   uv selected this interpreter via UV_PROJECT_ENVIRONMENT (it is not\n"
            "   under backend/.venv), so shortening the checkout will not help.\n"
            "   Unset UV_PROJECT_ENVIRONMENT, or point it at a shorter, space-free\n"
            f"   directory so the selected interpreter is at most {INTERPRETER_BYTE_LIMIT} bytes and\n"
            "   contains no space, then re-run the deployment."
        )
    else:
        fix = (
            "   Deploy from a checkout whose path is shorter and space-free so that\n"
            f"   the backend .venv interpreter is at most {INTERPRETER_BYTE_LIMIT} bytes, then\n"
            "   re-run the deployment."
        )

    return (
        f"Backend runtime interpreter path {cause}.\n"
        f"   Path: {interpreter_path}\n"
        "\n"
        "   uv writes the venv's console scripts with a /bin/sh trampoline that\n"
        "   embeds this absolute local path instead of a portable shebang when the\n"
        f"   interpreter path exceeds {INTERPRETER_BYTE_LIMIT} bytes or contains a space. agentcore\n"
        "   launch packages those scripts into dependencies.zip, producing an\n"
        "   AgentCore runtime that cannot start (issue #517).\n"
        "\n"
        f"{fix}"
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

    if args.interpreter:
        interpreter_path = args.interpreter
        # A direct probe carries no backend context, so remediation uses the
        # default-checkout wording rather than guessing at an override.
        remediation_backend_dir: Path | None = None
    else:
        interpreter_path = resolve_runtime_interpreter(args.backend_dir)
        remediation_backend_dir = args.backend_dir

    if venv_python_is_bootable(interpreter_path):
        return 0

    sys.stderr.write("\u274c " + remediation_message(interpreter_path, backend_dir=remediation_backend_dir) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
