#!/usr/bin/env python3
"""Preflight guard against an unbootable long-path AgentCore runtime (issue #517).

``agentcore launch`` (AgentCore starter toolkit) does not ship the backend
venv's console scripts. It rebuilds a dependency bundle with its own
``uv pip install --target <dir> --python-version <X.Y> --python-platform
aarch64-manylinux2014 --only-binary :all: --upgrade -r <reqs>`` and zips the
resulting ``<dir>`` into ``backend/.bedrock_agentcore/gameagentruntime/
dependencies.zip``. That ``--target`` install runs inside the uv-selected
project environment (``VIRTUAL_ENV`` is set by ``uv run``), so uv selects the
environment interpreter and embeds its path into every console script's launcher
line (for example the ``opentelemetry-instrument`` entrypoint). The embedded
basename depends on the uv version: some versions embed ``<env>/bin/python3``
and others ``<env>/bin/python``. This guard measures ``<env>/bin/python3``, the
longer of the names uv may embed, so the byte check is conservative under every
uv version.

uv generates each console script's first line with the same rule as
pip/distlib: on POSIX it embeds a ``#!<interpreter>`` shebang, but when that
shebang would be *non-simple* it falls back to a ``/bin/sh`` exec trampoline
that embeds the absolute LOCAL interpreter path. The toolkit's shebang rewriter
only normalizes single-line ``#!`` shebangs that name a python interpreter, so
the ``#!/bin/sh`` trampoline ships unchanged and the runtime dies at exec inside
the read-only container::

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

Three facts drive this guard:

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
3. The path that matters is the **interpreter name uv embeds** into the console
   scripts the toolkit's ``--target`` install writes. The embedded basename
   depends on the uv version the toolkit runs: some uv versions embed
   ``<env>/bin/python3`` and others ``<env>/bin/python``. This guard measures
   ``<env>/bin/python3`` -- the longer of the names uv may embed -- so the byte
   check stays conservative under every uv version (one extra byte can only make
   the guard stricter, never fail it open). uv lets ``UV_PROJECT_ENVIRONMENT``
   redirect that environment away from ``<backend>/.venv``, so a guard that
   reconstructs a path from the backend directory can measure a *different*
   interpreter than the one that ships -- passing a short synthetic path while
   uv packages a long real one. Both deploy paths invoke this guard with
   ``uv run python`` from inside the physical backend directory, which runs it
   *inside* that selected environment, so ``sys.prefix`` is the environment uv
   packages. The guard derives the environment from ``sys.prefix`` and measures
   ``<env>/bin/python3``, whose byte length decides uv's shebang-vs-trampoline
   choice. The backend ``.venv`` reconstruction is only a fallback for a
   non-uv invocation.

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
    wrongly pass exactly that path.

    ``os_name`` defaults to the running ``os.name``: the deploy host that runs
    ``agentcore launch`` is the host whose uv packages the dependency bundle, so
    its OS is authoritative for whether the shipped scripts are launchers or
    shebangs.
    """
    resolved_os_name = os_name if os_name is not None else os.name
    return resolved_os_name == "nt"


def venv_python_is_bootable(interpreter_path: str, *, os_name: str | None = None) -> bool:
    """Return True when uv would emit a portable, bootable console script.

    This predicate models uv's POSIX ``format_shebang`` rule only. On POSIX it
    returns False exactly for the case uv turns into the unbootable ``/bin/sh``
    trampoline: an interpreter path over 124 filesystem bytes, or one containing
    a space. On a native-Windows target uv does not apply that POSIX rule (it
    wraps console scripts in a native launcher binary), so this POSIX
    length/space check does not apply and the function returns True; what a
    Windows deploy host actually packages is out of this guard's scope.
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
    ``.venv/Scripts/python.exe`` on Windows). It uses the ``python3`` basename --
    the longer of the names uv may embed into the console scripts -- so the byte
    check stays conservative. It must NOT be used when the guard runs inside the
    uv-selected environment, because ``UV_PROJECT_ENVIRONMENT`` can point the
    real environment elsewhere; use :func:`resolve_runtime_interpreter` for the
    authoritative selection.

    The path is returned absolute and unresolved: uv embeds the path as invoked,
    and symlink resolution could mask the real on-disk length that ships in the
    trampoline.
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


def _embedded_interpreter_name_for_prefix(prefix: str) -> str:
    """Interpreter path uv embeds into console scripts for environment ``prefix``.

    ``agentcore launch`` rebuilds the dependency bundle with ``uv pip install
    --target`` inside the project environment, and uv writes each POSIX console
    script's launcher line against the environment's interpreter -- keying its
    shebang-vs-trampoline choice on that path's byte length. The embedded
    basename depends on the uv version (some embed ``python3``, some ``python``),
    so this returns ``<prefix>/bin/python3``, the longer of the names uv may
    embed; one extra byte can only make the guard stricter, never fail it open.
    ``sys.prefix`` is the environment uv selected and packages -- including a
    ``UV_PROJECT_ENVIRONMENT`` redirect -- so ``<prefix>/bin/python3`` is the
    path whose length decides whether the shipped script is a portable shebang or
    an unbootable ``/bin/sh`` trampoline. On Windows the native launcher makes
    the name immaterial; ``Scripts/python.exe`` is returned for symmetry.
    """
    prefix_path = Path(os.path.abspath(prefix))
    if os.name == "nt":
        return os.path.abspath(prefix_path / "Scripts" / "python.exe")
    return os.path.abspath(prefix_path / "bin" / "python3")


def resolve_runtime_interpreter(backend_dir: Path) -> str:
    """Resolve the interpreter path uv embeds into the packaged console scripts.

    When the guard is executing inside the uv-selected project environment,
    derive the environment from ``sys.prefix`` (which honors any
    ``UV_PROJECT_ENVIRONMENT`` redirect) and return the ``<env>/bin/python3``
    interpreter name -- the longer of the names uv may embed into the console
    scripts that ``agentcore launch`` packages via its ``uv pip install
    --target`` rebuild, whose byte length decides uv's shebang-vs-trampoline
    choice. Only when the guard is *not* running inside that environment (a bare
    ``python3`` fallback before the venv is usable) is the deterministic
    ``.venv`` path reconstructed instead.

    Returned absolute and unresolved (see :func:`resolve_venv_interpreter`).
    """
    if _running_in_project_environment(backend_dir):
        return _embedded_interpreter_name_for_prefix(sys.prefix)
    return resolve_venv_interpreter(backend_dir)


def _interpreter_is_external_override(interpreter_path: str, backend_dir: Path | None) -> bool:
    """True when ``interpreter_path`` lives outside ``<backend>/.venv``.

    When uv honors ``UV_PROJECT_ENVIRONMENT`` the selected interpreter can be an
    absolute path with no relationship to ``<backend>/.venv``. Shortening the
    checkout cannot repair such a path, so remediation must be tailored. When
    ``backend_dir`` is unknown (a direct ``--interpreter`` probe) we cannot make
    the determination and treat the path as the default-layout case.

    Only the containing *directories* are canonicalized with ``realpath``, never
    the interpreter file itself. ``uv run`` can present the project (and
    therefore ``sys.prefix``) through a symlinked checkout path, while
    ``agentcore launch`` resolves the project from the physical ``getcwd()``;
    resolving the directories keeps the default ``.venv`` case from being
    misreported as an external override when the only difference is a symlink in
    the checkout path. The interpreter file ``<env>/bin/python3`` is itself a
    symlink to the base interpreter outside the environment, so resolving it
    would walk out of ``.venv`` and make every real default-``.venv`` rejection
    look like an external override.
    """
    if backend_dir is None:
        return False
    default_venv = Path(os.path.realpath(backend_dir / ".venv"))
    interpreter = Path(interpreter_path)
    measured = Path(os.path.realpath(interpreter.parent)) / interpreter.name
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
            "   re-run the deployment. A dependency bundle cached from an earlier\n"
            "   deploy can outlive the move, so also delete\n"
            "   backend/.bedrock_agentcore/gameagentruntime/dependencies.zip to force\n"
            "   a rebuild (the deploy scripts do not forward --force-rebuild-deps;\n"
            "   that flag only applies to a manual agentcore launch)."
        )

    return (
        f"Backend runtime interpreter path {cause}.\n"
        f"   Path: {interpreter_path}\n"
        "\n"
        "   uv writes the packaged console scripts with a /bin/sh trampoline that\n"
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
