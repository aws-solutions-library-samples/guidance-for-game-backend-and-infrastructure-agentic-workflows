"""Regression tests for the AgentCore runtime path-length preflight (issue #517).

These tests prove behavior, not restate assumptions:

* ``RuntimePathPredicate`` exercises the real shared guard
  (``scripts/infrastructure/check_runtime_path.py``) against uv 0.12.0's exact
  ``format_shebang`` rule, verified from uv source:
  ``crates/uv-install-wheel/src/wheel.rs``. The ``/bin/sh`` trampoline fires on
  POSIX when ``2 + executable.len() + 1 > 127`` (interpreter path over 124
  **filesystem bytes**) OR the path contains a space. On native Windows uv emits
  a native launcher binary, so the predicate must never reject a Windows path.

* ``RuntimePathCli`` proves the CLI exits non-zero with an actionable message on
  an unbootable path and zero on a bootable one — the fail-fast contract both
  deploy paths rely on.

* ``UvPackagedEntrypointBoundary`` proves the guard against the artifact
  ``agentcore launch`` actually ships. ``agentcore launch`` does not package the
  venv's console scripts: the AgentCore starter toolkit rebuilds a dependency
  bundle with ``uv pip install --target <t> --python-platform
  aarch64-manylinux2014 --only-binary :all: -r <reqs>``, run inside the
  uv-selected project environment, and zips ``<t>`` into dependencies.zip. These
  tests reproduce that oracle offline with a local wheel that ships a console
  script, read the FIRST LINE uv wrote into the packaged script, and require the
  guard verdict to agree with uv's shebang/trampoline choice at
  ``<env>/bin/python3`` = 124 and 125 bytes. This is the artifact boundary that
  caused #517.

* ``RuntimeInterpreterSelection`` pins which interpreter name the guard measures
  (``<env>/bin/python3``, the longer of the names uv may embed) and that the
  environment is derived from ``sys.prefix`` so ``UV_PROJECT_ENVIRONMENT`` is
  honored.

* ``RemediationMessage`` proves the failure text matches where the unbootable
  interpreter came from — shorten-the-checkout for a default ``.venv`` path, but
  unset/relocate ``UV_PROJECT_ENVIRONMENT`` for an external uv-selected env — and
  that a symlinked checkout path is not misreported as an external override.

* ``PreflightOrdering`` proves the guard runs AFTER local dependency setup but
  BEFORE the first AWS-mutating command in both ``deploy.sh`` and
  ``Deploy-GameAgent.ps1``. A guard placed only before ``agentcore launch`` would
  run long after the first ``aws cloudformation deploy``. The check fails if the
  guard is moved after any mutator or removed.
"""

from __future__ import annotations

# Standard library
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GUARD_PY = REPOSITORY_ROOT / "scripts" / "infrastructure" / "check_runtime_path.py"
DEPLOY_SH = REPOSITORY_ROOT / "scripts" / "deploy.sh"
DEPLOY_PS1 = REPOSITORY_ROOT / "scripts" / "powershell" / "Public" / "Deploy-GameAgent.ps1"
GUARD_PS1 = REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Test-GameAgentRuntimePath.ps1"

# uv's interpreter-path byte ceiling (2 + len + 1 > 127  <=>  len > 124).
INTERPRETER_BYTE_LIMIT = 124


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("check_runtime_path", GUARD_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard_module()


class RuntimePathPredicate(unittest.TestCase):
    """uv 0.12.0 format_shebang rule, exercised against the shipped guard."""

    def test_at_byte_limit_is_bootable(self) -> None:
        # Exactly 124 bytes -> shebang line is 127 -> uv keeps the portable `#!`.
        path = "/" + "a" * (INTERPRETER_BYTE_LIMIT - 1)
        self.assertEqual(guard.interpreter_path_bytes(path), INTERPRETER_BYTE_LIMIT)
        self.assertTrue(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_one_byte_over_limit_is_unbootable(self) -> None:
        # 125 bytes -> shebang line is 128 (> 127) -> uv emits the trampoline.
        path = "/" + "a" * INTERPRETER_BYTE_LIMIT
        self.assertEqual(guard.interpreter_path_bytes(path), INTERPRETER_BYTE_LIMIT + 1)
        self.assertFalse(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_space_in_short_path_is_unbootable(self) -> None:
        # A space forces the trampoline regardless of length.
        path = "/Users/dev/My Project/backend/.venv/bin/python3"
        self.assertLessEqual(guard.interpreter_path_bytes(path), INTERPRETER_BYTE_LIMIT)
        self.assertFalse(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_multibyte_counted_as_filesystem_bytes_not_characters(self) -> None:
        # 62 x 'é' (2 bytes each) + "/p" = 127 bytes but only 65 characters.
        # uv measures bytes, so this is unbootable; a char-count guard would
        # wrongly pass it. This is the regression that pins byte-counting.
        path = "/" + "é" * 62 + "/p"
        self.assertEqual(len(path), 65)
        self.assertEqual(guard.interpreter_path_bytes(path), 127)
        self.assertFalse(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_native_windows_launcher_is_always_bootable(self) -> None:
        # On Windows uv emits a native launcher binary; the POSIX shebang
        # trampoline never applies, so even a very long .exe path is bootable.
        path = "C:\\" + "a" * 300 + "\\Scripts\\python.exe"
        self.assertGreater(guard.interpreter_path_bytes(path), INTERPRETER_BYTE_LIMIT)
        self.assertTrue(guard.venv_python_is_bootable(path, os_name="nt"))

    def test_posix_host_rejects_long_exe_path_no_suffix_shortcut(self) -> None:
        # A POSIX host never emits a Windows launcher. uv's gate is
        # `os_name == "posix"` only, so a long `.exe` interpreter path on POSIX
        # still gets the unbootable `/bin/sh` trampoline. The guard must NOT
        # treat the `.exe` suffix as a launcher and wrongly pass it.
        path = "/" + "a" * 300 + "/Scripts/python.exe"
        self.assertGreater(guard.interpreter_path_bytes(path), INTERPRETER_BYTE_LIMIT)
        self.assertFalse(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_posix_host_rejects_space_exe_path(self) -> None:
        # The same for a short space-bearing `.exe` path on POSIX: uv trampolines
        # on the space; the `.exe` suffix must not short-circuit the check.
        path = "/opt/My App/.venv/Scripts/python.exe"
        self.assertLessEqual(guard.interpreter_path_bytes(path), INTERPRETER_BYTE_LIMIT)
        self.assertFalse(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_launcher_decision_is_os_only_not_path(self) -> None:
        # The launcher decision is a pure function of the OS uv runs under, not
        # of the interpreter path string.
        self.assertTrue(guard.uses_native_windows_launcher("nt"))
        self.assertFalse(guard.uses_native_windows_launcher("posix"))

    def test_windows_space_in_launcher_is_bootable(self) -> None:
        # Spaces are fatal only for the POSIX shebang; the Windows launcher
        # binary handles them, so "C:\\Program Files\\..." must not be rejected.
        path = "C:\\Program Files\\game\\.venv\\Scripts\\python.exe"
        self.assertTrue(guard.venv_python_is_bootable(path, os_name="nt"))

    def test_resolved_venv_interpreter_is_absolute(self) -> None:
        resolved = guard.resolve_venv_interpreter(REPOSITORY_ROOT / "backend")
        self.assertTrue(Path(resolved).is_absolute())
        self.assertIn(".venv", resolved)

    def test_fallback_measures_the_embedded_python3_name(self) -> None:
        # The non-uv fallback reconstructs the `python3` name — the longer of the
        # names uv may embed — not the one-byte-shorter `python`.
        resolved = guard.resolve_venv_interpreter(Path("/srv/app/backend"))
        self.assertTrue(resolved.endswith("/bin/python3"), resolved)


class RuntimeInterpreterSelection(unittest.TestCase):
    """The guard must measure the interpreter uv actually embeds, not a
    reconstructed ``<backend>/.venv`` path.

    uv 0.12.0 honors ``UV_PROJECT_ENVIRONMENT``, so the real project environment
    (the one whose interpreter path the toolkit's ``--target`` install embeds)
    can live outside ``<backend>/.venv``. Both deploy paths run this guard via
    ``uv run`` from the backend project, which executes it *inside* that selected
    environment, so ``sys.prefix`` is the environment uv packages. The toolkit's
    ``uv pip install --target`` embeds the ``<env>/bin/python3`` name into the
    packaged console scripts and keys its shebang-vs-trampoline choice on that
    path, while ``uv run ... python`` reports ``sys.executable`` as
    ``<env>/bin/python3`` or ``<env>/bin/python`` depending on the uv version.
    The guard measures ``<env>/bin/python3`` -- the longer of the names uv may
    embed -- from ``sys.prefix``, not from ``sys.executable``. These tests pin
    that selection by controlling ``sys.prefix``, ``sys.executable``, and
    ``VIRTUAL_ENV`` exactly as ``uv run`` sets them -- no uv or real venv needed.
    """

    def setUp(self) -> None:
        self._orig_executable = guard.sys.executable
        self._orig_prefix = guard.sys.prefix
        self._orig_virtual_env = os.environ.get("VIRTUAL_ENV")

    def tearDown(self) -> None:
        guard.sys.executable = self._orig_executable
        guard.sys.prefix = self._orig_prefix
        if self._orig_virtual_env is None:
            os.environ.pop("VIRTUAL_ENV", None)
        else:
            os.environ["VIRTUAL_ENV"] = self._orig_virtual_env

    def _simulate_uv_run(self, env_dir: str) -> None:
        """Mimic ``uv run ... python``: VIRTUAL_ENV and sys.prefix are the env.
        sys.executable is set to the ``<env>/bin/python3`` spelling here, but the
        guard derives the measured name from sys.prefix, so the executable
        basename a given uv version reports does not change the result."""
        os.environ["VIRTUAL_ENV"] = env_dir
        guard.sys.prefix = env_dir
        guard.sys.executable = str(Path(env_dir) / "bin" / "python3")

    def test_long_project_environment_overrides_short_backend_venv(self) -> None:
        # A SHORT backend/.venv path (a reconstructing guard would pass it) while
        # uv actually selected a LONG project environment via
        # UV_PROJECT_ENVIRONMENT. The guard must measure the python3 name uv
        # embeds for the real selected environment and REJECT it.
        short_backend = Path("/srv/app/backend")
        self.assertLessEqual(
            guard.interpreter_path_bytes(guard.resolve_venv_interpreter(short_backend)),
            INTERPRETER_BYTE_LIMIT,
            "precondition: the reconstructed backend/.venv path is short enough to pass",
        )
        long_env = "/" + "e" * 200 + "/env"
        self._simulate_uv_run(long_env)

        resolved = guard.resolve_runtime_interpreter(short_backend)
        self.assertTrue(resolved.startswith(long_env), resolved)
        # The embedded python3 name is measured, not a reconstructed backend path.
        self.assertTrue(resolved.endswith("/bin/python3"), resolved)
        self.assertGreater(guard.interpreter_path_bytes(resolved), INTERPRETER_BYTE_LIMIT)
        self.assertFalse(
            guard.venv_python_is_bootable(resolved, os_name="posix"),
            "guard must reject the real long project-environment interpreter",
        )

    def test_short_project_environment_is_not_falsely_rejected(self) -> None:
        # The inverse: uv selected a SHORT environment. The guard must accept it
        # even if some other path on disk is long. Prevents a false rejection.
        short_env = "/srv/app/.venv"
        self._simulate_uv_run(short_env)
        resolved = guard.resolve_runtime_interpreter(Path("/srv/app/backend"))
        self.assertTrue(resolved.startswith(short_env), resolved)
        self.assertTrue(guard.venv_python_is_bootable(resolved, os_name="posix"))

    def test_embedded_python3_name_is_measured(self) -> None:
        # The guard measures `<env>/bin/python3`, the longer of the names uv may
        # embed. At <env>/bin/python == 124 and python3 == 125 the
        # packaged artifact trampolines (uv embeds python3), so the guard must
        # measure python3 (125, over the limit) and REJECT -- not the one-byte
        # shorter python (124) which would falsely pass.
        env_dir = "/" + "e" * 108 + "/env"  # <env>/bin/python == 124, python3 == 125
        self._simulate_uv_run(env_dir)
        self.assertEqual(guard.interpreter_path_bytes(str(Path(env_dir) / "bin" / "python")), INTERPRETER_BYTE_LIMIT)
        self.assertEqual(
            guard.interpreter_path_bytes(str(Path(env_dir) / "bin" / "python3")), INTERPRETER_BYTE_LIMIT + 1
        )
        resolved = guard.resolve_runtime_interpreter(Path("/srv/app/backend"))
        self.assertTrue(resolved.endswith("/bin/python3"), resolved)
        self.assertEqual(guard.interpreter_path_bytes(resolved), INTERPRETER_BYTE_LIMIT + 1)
        self.assertFalse(guard.venv_python_is_bootable(resolved, os_name="posix"))

    def test_embedded_python3_at_limit_is_bootable(self) -> None:
        # When the embedded `python3` name is exactly 124 bytes uv writes a
        # portable shebang; the guard must accept it. python3 == 124 here.
        env_dir = "/" + "e" * 107 + "/env"  # <env>/bin/python3 == 124
        self._simulate_uv_run(env_dir)
        self.assertEqual(guard.interpreter_path_bytes(str(Path(env_dir) / "bin" / "python3")), INTERPRETER_BYTE_LIMIT)
        resolved = guard.resolve_runtime_interpreter(Path("/srv/app/backend"))
        self.assertTrue(resolved.endswith("/bin/python3"), resolved)
        self.assertEqual(guard.interpreter_path_bytes(resolved), INTERPRETER_BYTE_LIMIT)
        self.assertTrue(guard.venv_python_is_bootable(resolved, os_name="posix"))

    def test_falls_back_to_backend_venv_outside_project_environment(self) -> None:
        # A bare system python3 (no VIRTUAL_ENV, executable not under
        # backend/.venv) is not trusted to represent the runtime, so the guard
        # falls back to the deterministic backend/.venv reconstruction.
        os.environ.pop("VIRTUAL_ENV", None)
        guard.sys.executable = "/usr/bin/python3"
        guard.sys.prefix = "/usr"
        backend = REPOSITORY_ROOT / "backend"
        resolved = guard.resolve_runtime_interpreter(backend)
        self.assertEqual(resolved, guard.resolve_venv_interpreter(backend))

    def test_default_venv_layout_is_trusted_without_virtual_env(self) -> None:
        # A direct `<backend>/.venv/bin/python3 check_runtime_path.py` call sets
        # no VIRTUAL_ENV but still runs the real runtime interpreter; the guard
        # must trust it via the default-layout check and measure the embedded
        # python3 name derived from the environment prefix.
        os.environ.pop("VIRTUAL_ENV", None)
        backend = Path("/srv/app/backend")
        guard.sys.prefix = str(backend / ".venv")
        guard.sys.executable = str(backend / ".venv" / "bin" / "python3")
        resolved = guard.resolve_runtime_interpreter(backend)
        self.assertEqual(resolved, os.path.abspath(guard.sys.executable))

    def test_resolver_measures_python3_even_when_executable_is_python(self) -> None:
        # Pins the decision to derive the measured name from sys.prefix rather
        # than sys.executable. uv 0.12.22 and later report `uv run python` as
        # `<env>/bin/python` (one byte shorter than the `python3` path the
        # toolkit's `--target` install packages), so a resolver that returned
        # `os.path.abspath(sys.executable)` would measure the shorter name and
        # pass a 125-byte `python3` path that the packaged artifact trampolines.
        # The resolver must return `<env>/bin/python3` regardless of the
        # executable basename uv's child process reports.
        env_dir = "/srv/app/.venv"
        os.environ["VIRTUAL_ENV"] = env_dir
        guard.sys.prefix = env_dir
        guard.sys.executable = str(Path(env_dir) / "bin" / "python")
        resolved = guard.resolve_runtime_interpreter(Path("/srv/app/backend"))
        self.assertEqual(resolved, str(Path(env_dir) / "bin" / "python3"))
        self.assertTrue(resolved.endswith("/bin/python3"), resolved)


class RemediationMessage(unittest.TestCase):
    """Remediation must match where the unbootable interpreter came from.

    A path under the default ``<backend>/.venv`` is repaired by a shorter
    checkout (or a short, space-free absolute ``UV_PROJECT_ENVIRONMENT``). An
    interpreter uv selected via an absolute ``UV_PROJECT_ENVIRONMENT`` (outside
    ``<backend>/.venv``) is not fixed by moving the checkout, so the message
    must point at the override instead. Advising only a shorter checkout is
    wrong for an external environment.
    """

    # Phrase that appears only in the external-override remedy branch.
    OVERRIDE_MARKER = "uv selected this interpreter via UV_PROJECT_ENVIRONMENT"

    def test_default_venv_path_advises_shortening_checkout(self) -> None:
        backend = Path("/srv/app/backend")
        interp = str(backend / ".venv" / "bin" / ("p" * 200))
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("shorter", msg.lower())
        # The default-checkout branch is not the external-override branch.
        self.assertNotIn(self.OVERRIDE_MARKER, msg)

    def test_default_venv_path_also_offers_short_project_environment(self) -> None:
        # The default-checkout remedy also offers a short, space-free absolute
        # UV_PROJECT_ENVIRONMENT, which keeps backend/.env.local and
        # .bedrock_agentcore.yaml in place.
        backend = Path("/srv/app/backend")
        interp = str(backend / ".venv" / "bin" / ("p" * 200))
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("UV_PROJECT_ENVIRONMENT", msg)
        self.assertRegex(msg, r"short.*absolute")

    def test_default_venv_path_mentions_cached_dependencies_zip(self) -> None:
        # A dependency bundle cached from an earlier deploy can outlive a move to
        # a shorter path, so the default-checkout remedy must mention deleting
        # the cached dependencies.zip or forcing a rebuild.
        backend = Path("/srv/app/backend")
        interp = str(backend / ".venv" / "bin" / ("p" * 200))
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("dependencies.zip", msg)
        self.assertIn("--force-rebuild-deps", msg)

    def test_external_environment_advises_unsetting_override(self) -> None:
        # The interpreter uv selected lives outside backend/.venv -> override.
        backend = Path("/srv/app/backend")
        interp = "/" + "e" * 200 + "/bin/python3"
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("UV_PROJECT_ENVIRONMENT", msg)
        self.assertIn(self.OVERRIDE_MARKER, msg)
        # Must not tell the user to simply move the checkout for an override.
        self.assertNotRegex(msg.lower(), r"deploy from a checkout")

    def test_external_environment_does_not_claim_checkout_cannot_help(self) -> None:
        # A relative UV_PROJECT_ENVIRONMENT resolves under backend/, so the
        # override remedy must not claim shortening the checkout cannot help.
        backend = Path("/srv/app/backend")
        interp = "/" + "e" * 200 + "/bin/python3"
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertNotRegex(msg.lower(), r"will not help")

    def test_external_environment_mentions_cached_dependencies_zip(self) -> None:
        # The override remedy carries the same cached-bundle advice as the
        # default-checkout remedy.
        backend = Path("/srv/app/backend")
        interp = "/" + "e" * 200 + "/bin/python3"
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("dependencies.zip", msg)
        self.assertIn("--force-rebuild-deps", msg)

    def test_external_detection_true_for_outside_path(self) -> None:
        backend = Path("/srv/app/backend")
        self.assertTrue(guard._interpreter_is_external_override("/other/env/bin/python3", backend))

    def test_external_detection_false_for_default_venv_path(self) -> None:
        backend = Path("/srv/app/backend")
        interp = str(backend / ".venv" / "bin" / "python3")
        self.assertFalse(guard._interpreter_is_external_override(interp, backend))

    def test_external_detection_false_without_backend_context(self) -> None:
        # A direct --interpreter probe carries no backend context; the override
        # branch must not fire (we cannot know, so default-checkout wording).
        self.assertFalse(guard._interpreter_is_external_override("/other/env/bin/python3", None))

    def _make_venv_with_external_symlinked_python3(self, root: Path, venv_parent: Path) -> tuple[Path, str]:
        """Build a real venv-like layout whose ``bin/python3`` is a symlink to a
        base interpreter OUTSIDE the venv (as uv creates it).

        Returns ``(backend_dir, interpreter_path)`` where ``interpreter_path`` is
        the unresolved ``<venv>/bin/python3`` the guard measures. ``root`` holds
        the external base interpreter; ``venv_parent`` is the directory that
        contains ``.venv`` (the backend directory).
        """
        base_bin = root / "base" / "bin"
        base_bin.mkdir(parents=True)
        base_interp = base_bin / "python3.13"
        base_interp.write_text("#!/bin/sh\n", encoding="utf-8")
        venv_bin = venv_parent / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        interpreter = venv_bin / "python3"
        os.symlink(base_interp, interpreter)
        return venv_parent, str(interpreter)

    def test_default_venv_with_symlinked_python3_is_not_override(self) -> None:
        # A real default `.venv` whose `bin/python3` is a symlink to a base
        # interpreter outside the venv (how uv builds it) must get the
        # default-checkout remedy, not the override remedy. Resolving the
        # interpreter symlink would walk out of `.venv` and misreport it.
        with tempfile.TemporaryDirectory(prefix="rt_default_") as td:
            root = Path(td)
            backend = root / "backend"
            backend.mkdir()
            backend, interp = self._make_venv_with_external_symlinked_python3(root, backend)
            self.assertFalse(
                guard._interpreter_is_external_override(interp, backend),
                "a real default .venv with a symlinked python3 must not be an override",
            )
            msg = guard.remediation_message(interp, backend_dir=backend)
            self.assertIn("shorter", msg.lower())
            self.assertIn("dependencies.zip", msg)
            self.assertNotIn(self.OVERRIDE_MARKER, msg)

    def test_symlinked_checkout_default_venv_with_symlinked_python3_is_not_override(self) -> None:
        # The checkout path itself is a symlink (logical spelling) while the
        # interpreter is measured through the physical path, and `bin/python3`
        # is a real symlink to an external base interpreter. The override branch
        # must NOT fire: resolving only the directories collapses the checkout
        # symlink, and the interpreter file is never resolved.
        with tempfile.TemporaryDirectory(prefix="rt_symlink_") as td:
            real_root = Path(td) / "real"
            backend = real_root / "backend"
            backend.mkdir(parents=True)
            self._make_venv_with_external_symlinked_python3(Path(td), backend)
            link_root = Path(td) / "link"
            os.symlink(real_root, link_root)
            logical_backend = link_root / "backend"
            physical_interp = str(backend / ".venv" / "bin" / "python3")
            self.assertFalse(
                guard._interpreter_is_external_override(physical_interp, logical_backend),
                "a symlinked default .venv must not be reported as an external override",
            )
            msg = guard.remediation_message(physical_interp, backend_dir=logical_backend)
            self.assertIn("shorter", msg.lower())
            self.assertNotIn(self.OVERRIDE_MARKER, msg)

    def test_external_project_environment_with_symlinked_python3_is_override(self) -> None:
        # An external UV_PROJECT_ENVIRONMENT whose `bin/python3` is a real
        # symlink to a base interpreter: the environment is genuinely outside
        # `<backend>/.venv`, so the override remedy must fire.
        with tempfile.TemporaryDirectory(prefix="rt_external_") as td:
            root = Path(td)
            backend = root / "backend"
            backend.mkdir()
            (backend / ".venv" / "bin").mkdir(parents=True)
            # The selected environment lives elsewhere under the same root.
            _, interp = self._make_venv_with_external_symlinked_python3(root, root / "external-env-parent")
            self.assertTrue(
                guard._interpreter_is_external_override(interp, backend),
                "an interpreter outside backend/.venv must be reported as an override",
            )
            msg = guard.remediation_message(interp, backend_dir=backend)
            self.assertIn("UV_PROJECT_ENVIRONMENT", msg)
            self.assertNotRegex(msg.lower(), r"deploy from a checkout")


class RuntimePathCli(unittest.TestCase):
    """The fail-fast CLI contract the deploy scripts invoke."""

    def _run(self, interpreter: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(GUARD_PY), "--interpreter", interpreter],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_bootable_path_exits_zero(self) -> None:
        result = self._run("/srv/app/backend/.venv/bin/python3")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_over_limit_path_exits_nonzero_with_actionable_message(self) -> None:
        result = self._run("/" + "a" * 200 + "/python3")
        self.assertNotEqual(result.returncode, 0)
        message = result.stdout + result.stderr
        # Names the real limit (bytes, not "127 characters") and a concrete fix.
        self.assertIn("124", message)
        self.assertIn("byte", message.lower())
        self.assertRegex(message.lower(), r"shorter|re-run")

    def test_space_path_exits_nonzero_and_names_the_space(self) -> None:
        result = self._run("/opt/My App/backend/.venv/bin/python3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("space", (result.stdout + result.stderr).lower())

    def _run_simulated_os(self, *, os_name: str, interpreter: str) -> subprocess.CompletedProcess:
        """Invoke the CLI with the native-Windows predicate forced to the given
        ``os_name`` result, so the skip branch can be exercised on a POSIX host
        without a real Windows machine. We patch ``uses_native_windows_launcher``
        rather than flipping ``os.name`` globally (which would make the stdlib
        try to ``import nt``). Passes ``--interpreter`` so no venv is resolved;
        the skip must take precedence over measuring that path.
        """
        force_windows = "True" if os_name == "nt" else "False"
        bootstrap = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('check_runtime_path', {str(GUARD_PY)!r});"
            "mod = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(mod);"
            f"mod.uses_native_windows_launcher = lambda os_name=None: {force_windows};"
            f"sys.exit(mod.main(['--interpreter', {interpreter!r}]))"
        )
        return subprocess.run(
            [sys.executable, "-c", bootstrap],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_native_windows_skips_with_distinct_exit_code_and_token(self) -> None:
        # On a native Windows host the guard measures nothing (out of scope,
        # tracked in issue #610). It must announce the skip on stdout with a
        # fixed machine-readable token the callers key on, print exactly one
        # human skip line, and exit with the dedicated SKIPPED code (not 0, so a
        # caller never mistakes a skip for a measured pass, and not 1, so the
        # deploy still continues).
        long_windows_path = "C:\\" + "a" * 300 + "\\Scripts\\python.exe"
        result = self._run_simulated_os(os_name="nt", interpreter=long_windows_path)
        self.assertEqual(result.returncode, guard.EXIT_SKIPPED_NATIVE_WINDOWS, result.stdout + result.stderr)
        self.assertNotEqual(guard.EXIT_SKIPPED_NATIVE_WINDOWS, 0)
        self.assertNotEqual(guard.EXIT_SKIPPED_NATIVE_WINDOWS, 1)
        self.assertIn(guard.SKIPPED_NATIVE_WINDOWS_TOKEN, result.stdout)
        self.assertIn("610", result.stdout)
        self.assertIn("native Windows", result.stdout)
        # Exactly one human-facing skip line, and no "portable"/success claim.
        self.assertNotIn("portable", result.stdout.lower())
        skip_lines = [line for line in result.stdout.splitlines() if "native Windows" in line]
        self.assertEqual(len(skip_lines), 1, result.stdout)

    def test_posix_measured_pass_emits_no_skip_token(self) -> None:
        # The inverse: a real measured pass on POSIX exits 0 and never emits the
        # skip token, so callers print the success line only for a measured pass.
        result = self._run_simulated_os(os_name="posix", interpreter="/srv/app/backend/.venv/bin/python3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(guard.SKIPPED_NATIVE_WINDOWS_TOKEN, result.stdout)

    def _run_default_path(self, *, virtual_env: str, executable_env: str) -> subprocess.CompletedProcess:
        """Invoke the CLI with NO ``--interpreter`` so it exercises the default
        ``resolve_runtime_interpreter`` path, with ``VIRTUAL_ENV`` and a
        bootstrap that points ``sys.prefix`` (and ``sys.executable``) at the
        simulated uv-selected environment, exactly as ``uv run`` presents it.
        """
        bootstrap = (
            "import os, sys, runpy;"
            f"sys.prefix = {virtual_env!r};"
            f"sys.executable = {executable_env!r};"
            f"sys.argv = [{str(GUARD_PY)!r}, '--backend-dir', '/srv/app/backend'];"
            f"runpy.run_path({str(GUARD_PY)!r}, run_name='__main__')"
        )
        env = dict(os.environ, VIRTUAL_ENV=virtual_env)
        return subprocess.run(
            [sys.executable, "-c", bootstrap],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_default_path_rejects_long_uv_selected_environment(self) -> None:
        # End-to-end: short backend dir, long uv-selected project environment.
        # The default resolution path (no --interpreter) must fail. This is the
        # short-backend, long-environment release path.
        long_env = "/" + "e" * 200 + "/env"
        result = self._run_default_path(
            virtual_env=long_env,
            executable_env=f"{long_env}/bin/python3",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("124", result.stdout + result.stderr)

    def test_default_path_accepts_short_uv_selected_environment(self) -> None:
        # Inverse: a short uv-selected environment must not be falsely rejected.
        short_env = "/srv/app/.venv"
        result = self._run_default_path(
            virtual_env=short_env,
            executable_env=f"{short_env}/bin/python3",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def _uv_available() -> bool:
    try:
        subprocess.run(["uv", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@unittest.skipUnless(_uv_available(), "uv is required to generate real console scripts")
class UvPackagedEntrypointBoundary(unittest.TestCase):
    """Prove the guard against the artifact ``agentcore launch`` actually ships.

    ``agentcore launch`` does not package the venv's console scripts. The
    AgentCore starter toolkit rebuilds a dependency bundle with ``uv pip install
    --target <t> --python-version <X.Y> --python-platform aarch64-manylinux2014
    --only-binary :all: --upgrade -r <reqs>``, run inside the uv-selected project
    environment, and zips ``<t>`` into dependencies.zip. uv's ``format_shebang``
    decides at install time whether each console script is a portable ``#!``
    shebang or an unbootable ``/bin/sh`` trampoline embedding the environment
    interpreter path (the longer of the names uv may embed is
    ``<env>/bin/python3``).

    These tests reproduce that oracle offline: build a minimal local wheel (no
    network) that ships a console script, create a uv environment whose
    ``<env>/bin/python3`` path we control to 124 or 125 bytes, run the toolkit's
    ``--target`` install against that environment from the project directory,
    read the FIRST LINE uv wrote into the packaged script, and require the
    guard's verdict to agree with uv's artifact. If uv's rule ever shifts, these
    tests fail -- they are not a restatement of the guard's own arithmetic.

    Grounded in uv source: ``crates/uv-install-wheel/src/wheel.rs``
    ``format_shebang`` (identical rule across 0.10.x and 0.12.x): on POSIX the
    ``/bin/sh`` trampoline fires when ``2 + executable.len() + 1 > 127`` (path
    over 124 bytes) or the path contains a space.
    """

    _tmp: tempfile.TemporaryDirectory[str]
    _wheel: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="rt_guard_uv_")
        root = Path(cls._tmp.name)
        pkg = root / "samplepkg"
        (pkg / "samplepkg").mkdir(parents=True)
        (pkg / "pyproject.toml").write_text(
            "[project]\n"
            'name = "samplepkg"\n'
            'version = "0.0.1"\n'
            'requires-python = ">=3.8"\n'
            "[project.scripts]\n"
            'sampletool = "samplepkg:main"\n'
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n',
            encoding="utf-8",
        )
        (pkg / "samplepkg" / "__init__.py").write_text("def main():\n    print('hi')\n", encoding="utf-8")
        dist = root / "dist"
        proc = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(dist), str(pkg)],
            capture_output=True,
            text=True,
        )
        # uv is present (class skip guard), so a build failure is a real
        # regression, not a reason to silently skip: fail the setup.
        if proc.returncode != 0:
            raise AssertionError(f"uv build failed with uv present: {proc.stderr}")
        wheels = list(dist.glob("*.whl"))
        if not wheels:
            raise AssertionError("uv build produced no wheel with uv present")
        cls._wheel = wheels[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self._bases: list[Path] = []

    def tearDown(self) -> None:
        for base in self._bases:
            shutil.rmtree(base, ignore_errors=True)

    def _short_base(self) -> Path | None:
        """A short, process-unique base directory for building boundary envs.

        The 124/125-byte boundary leaves no room for a long system temp prefix
        (macOS ``/var/folders/...`` is ~50 bytes), so these tests need a short
        absolute root. Prefer ``/tmp`` (POSIX deploy hosts have it); fall back to
        the system temp dir. Returns ``None`` only when neither candidate exists
        or is writable; whether a usable base is still too long to construct the
        exact boundary path is decided by the caller.
        """
        for candidate in ("/tmp", tempfile.gettempdir()):
            root = Path(candidate)
            if not root.is_dir():
                continue
            try:
                base = Path(tempfile.mkdtemp(prefix="g524_", dir=str(root)))
            except OSError:
                continue
            self._bases.append(base)
            return base
        return None

    def _package_at_python3_bytes(self, python3_bytes: int) -> tuple[Path, bool, str, str | None] | None:
        """Reproduce the toolkit's packaging oracle for an env whose
        ``<env>/bin/python3`` path is exactly ``python3_bytes``.

        Returns ``(env_dir, uv_bootable, first_line, embedded_name)`` where
        ``uv_bootable`` is True when uv wrote a portable ``#!`` shebang into the
        packaged console script and False when it wrote the ``/bin/sh``
        trampoline, and ``embedded_name`` is the interpreter basename uv embedded
        (``python`` or ``python3``, or ``None`` if it could not be read). Returns
        ``None`` only when the short base is too long to construct the target
        boundary path (a size constraint, not a uv failure); any uv failure with
        uv present fails the test.
        """
        base = self._short_base()
        if base is None:
            return None
        real_base = Path(os.path.realpath(str(base)))
        tail = "/bin/python3"
        name_len = python3_bytes - len(os.fsencode(str(real_base))) - 1 - len(tail)
        if name_len < 1:
            return None
        env_dir = real_base / ("v" * name_len)
        if len(os.fsencode(str(env_dir / "bin" / "python3"))) != python3_bytes:
            return None

        # Project directory (what `agentcore launch` resolves the project from).
        project = real_base / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            "[project]\n" 'name = "proj"\n' 'version = "0.0.1"\n' 'requires-python = ">=3.8"\n',
            encoding="utf-8",
        )
        # Create the uv environment whose python3 path we control.
        created = subprocess.run(["uv", "venv", str(env_dir)], capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, f"uv venv failed: {created.stderr}")

        reqs = project / "requirements.txt"
        reqs.write_text(f"samplepkg @ {self._wheel.resolve().as_uri()}\n", encoding="utf-8")
        target = project / "package"
        target.mkdir()
        # The toolkit's _build_uv_command for a cross-compiled AgentCore build.
        cmd = [
            "uv",
            "pip",
            "install",
            "--target",
            str(target),
            "--python-version",
            f"{sys.version_info.major}.{sys.version_info.minor}",
            "--python-platform",
            "aarch64-manylinux2014",
            "--only-binary",
            ":all:",
            "--upgrade",
            "-r",
            str(reqs),
        ]
        # Run from the project directory inside the uv-selected environment:
        # VIRTUAL_ENV points at env_dir, so uv selects that environment and
        # embeds its interpreter path (the longer of the names it may embed is
        # <env>/bin/python3) into the packaged console script.
        env = dict(os.environ, VIRTUAL_ENV=str(env_dir))
        env.pop("UV_PROJECT_ENVIRONMENT", None)
        env["PATH"] = str(env_dir / "bin") + os.pathsep + env.get("PATH", "")
        installed = subprocess.run(cmd, cwd=str(project), capture_output=True, text=True, env=env)
        self.assertEqual(
            installed.returncode, 0, f"toolkit --target install failed with uv present: {installed.stderr}"
        )
        script = target / "bin" / "sampletool"
        self.assertTrue(script.exists(), "the --target install produced no console script")
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
        first_line = lines[0]
        uv_bootable = first_line.startswith("#!") and not first_line.startswith("#!/bin/sh")
        embedded_name = self._embedded_name_from_artifact(lines, env_dir)
        return env_dir, uv_bootable, first_line, embedded_name

    @staticmethod
    def _embedded_name_from_artifact(lines: list[str], env_dir: Path) -> str | None:
        """The interpreter basename uv embedded (``python`` or ``python3``).

        For a portable ``#!`` shebang uv may rewrite the embedded path, so read
        the basename from the shebang's path. For a ``/bin/sh`` trampoline the
        absolute interpreter path appears on a later ``exec`` line; find whichever
        line names ``<env>/bin/python`` or ``<env>/bin/python3`` and return that
        basename. Returns ``None`` when no embedded name can be read.
        """
        env_bin = str(env_dir / "bin")
        for candidate in ("python3", "python"):
            needle = f"{env_bin}/{candidate}"
            for line in lines:
                if needle in line:
                    return candidate
        # Portable shebang may name only the basename after a rewrite.
        for candidate in ("python3", "python"):
            for line in lines:
                if line.startswith("#!") and line.rstrip().endswith("/" + candidate):
                    return candidate
        return None

    def _guard_verdict_for_env(self, env_dir: Path) -> bool:
        """The guard's bootable verdict for the embedded ``<env>/bin/python3``."""
        embedded = guard._embedded_interpreter_name_for_prefix(str(env_dir))
        self.assertTrue(embedded.endswith("/bin/python3"), embedded)
        return bool(guard.venv_python_is_bootable(embedded, os_name="posix"))

    def _embedded_byte_length(self, env_dir: Path, embedded_name: str | None) -> int:
        """Byte length of the interpreter path uv actually embedded."""
        name = embedded_name or "python3"
        return len(os.fsencode(str(env_dir / "bin" / name)))

    def test_guard_matches_packaged_artifact_at_python3_124_bytes(self) -> None:
        # <env>/bin/python3 == 124 bytes: uv writes a portable shebang into the
        # packaged script -> BOOTABLE. The guard (measuring python3) must agree.
        made = self._package_at_python3_bytes(124)
        if made is None:
            self.skipTest("no short temp base to construct a 124-byte python3 path")
        env_dir, uv_bootable, first_line, embedded_name = made
        self.assertTrue(uv_bootable, f"expected a portable shebang at python3=124 bytes: {first_line!r}")
        self.assertEqual(
            self._guard_verdict_for_env(env_dir),
            uv_bootable,
            "guard verdict must match uv's packaged console script at python3=124 bytes",
        )

    def test_guard_matches_packaged_artifact_at_python3_125_bytes(self) -> None:
        # <env>/bin/python3 == 125 bytes. The guard measures python3 (the longer
        # of the names uv may embed), so the measured length is at least the
        # embedded length and the guard must REJECT whenever the artifact
        # trampolines -- independent of which basename this uv version embedded.
        #
        # The uv version decides the embedded name: uv up to 0.12.0 embeds
        # <env>/bin/python3 (125 bytes -> trampoline), while uv 0.12.22 embeds
        # <env>/bin/python (124 bytes -> portable shebang). Either way the guard
        # must not fail open; exact agreement (reject == trampoline) is required
        # only when uv embedded `python3`.
        made = self._package_at_python3_bytes(125)
        if made is None:
            self.skipTest("no short temp base to construct a 125-byte python3 path")
        env_dir, uv_bootable, first_line, embedded_name = made
        guard_bootable = self._guard_verdict_for_env(env_dir)

        # Safety property 1: the guard rejects whenever uv trampolined.
        if not uv_bootable:
            self.assertFalse(
                guard_bootable,
                f"guard must reject a trampolined artifact at python3=125 bytes: {first_line!r}",
            )
        # Safety property 2: the measured length (python3) is at least the
        # embedded length, so the guard is never one byte too lax.
        measured = len(os.fsencode(guard._embedded_interpreter_name_for_prefix(str(env_dir))))
        embedded = self._embedded_byte_length(env_dir, embedded_name)
        self.assertGreaterEqual(
            measured,
            embedded,
            f"measured python3 length {measured} must be >= embedded length {embedded}",
        )
        # Exact agreement only when uv embedded `python3` (125 bytes here).
        if embedded_name == "python3":
            self.assertFalse(uv_bootable, f"expected a /bin/sh trampoline at python3=125 bytes: {first_line!r}")
            self.assertEqual(
                guard_bootable,
                uv_bootable,
                "guard verdict must match uv's packaged console script when uv embeds python3",
            )

    def _run_guard_cli_in_project(self, env_dir: Path, project: Path) -> subprocess.CompletedProcess:
        """Run the guard CLI exactly as the deploy scripts do: `uv run python`
        from inside the physical project directory, with the controlled
        environment selected via VIRTUAL_ENV / PATH / UV_PROJECT_ENVIRONMENT."""
        env = dict(os.environ, VIRTUAL_ENV=str(env_dir), UV_PROJECT_ENVIRONMENT=str(env_dir))
        env["PATH"] = str(env_dir / "bin") + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            ["uv", "run", "python", str(GUARD_PY), "--backend-dir", str(project)],
            cwd=str(project),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_guard_cli_matches_packaged_artifact_at_python3_124_bytes(self) -> None:
        # Run the guard CLI (not just the helper) under `uv run` at 124 bytes:
        # uv writes a portable shebang -> the CLI must exit 0.
        made = self._package_at_python3_bytes(124)
        if made is None:
            self.skipTest("no short temp base to construct a 124-byte python3 path")
        env_dir, uv_bootable, first_line, _ = made
        self.assertTrue(uv_bootable, f"expected a portable shebang at python3=124 bytes: {first_line!r}")
        project = env_dir.parent / "proj"
        result = self._run_guard_cli_in_project(env_dir, project)
        self.assertEqual(result.returncode, 0, f"guard CLI must pass a 124-byte env: {result.stdout}{result.stderr}")

    def test_guard_cli_matches_packaged_artifact_at_python3_125_bytes(self) -> None:
        # Run the guard CLI under `uv run` at 125 bytes. The guard measures
        # python3, so it must reject whenever the artifact trampolines. When uv
        # embedded python3 the CLI must exit non-zero (exact agreement).
        made = self._package_at_python3_bytes(125)
        if made is None:
            self.skipTest("no short temp base to construct a 125-byte python3 path")
        env_dir, uv_bootable, first_line, embedded_name = made
        project = env_dir.parent / "proj"
        result = self._run_guard_cli_in_project(env_dir, project)
        if not uv_bootable:
            self.assertNotEqual(
                result.returncode,
                0,
                f"guard CLI must reject a trampolined 125-byte env: {result.stdout}{result.stderr}",
            )
        if embedded_name == "python3":
            self.assertNotEqual(result.returncode, 0, f"{result.stdout}{result.stderr}")
            self.assertIn("124", result.stdout + result.stderr)

    def test_guard_cli_rejects_real_long_external_project_environment(self) -> None:
        # End-to-end on the release path: a SHORT backend dir with a LONG
        # external UV_PROJECT_ENVIRONMENT. The guard is run exactly as the
        # deploy scripts run it -- `uv run` from the backend project -- so it
        # executes inside the uv-selected environment and measures the
        # <env>/bin/python3 uv embeds. uv selects the long external env; the
        # guard must fail. This exercises uv's own environment selection.
        base = self._short_base()
        if base is None:
            self.skipTest("no short temp base available")
        backend = Path(os.path.realpath(str(base))) / "backend"
        backend.mkdir()
        (backend / "pyproject.toml").write_text(
            "[project]\n" 'name = "be"\n' 'version = "0.0.1"\n' 'requires-python = ">=3.8"\n',
            encoding="utf-8",
        )
        long_env = Path(os.path.realpath(str(base))) / ("e" * 160)
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(long_env))
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(backend),
                "python",
                str(GUARD_PY),
                "--backend-dir",
                str(backend),
            ],
            cwd=str(backend),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(
            result.returncode,
            0,
            f"guard must reject the long uv-selected external environment: {result.stdout}{result.stderr}",
        )
        combined = result.stdout + result.stderr
        self.assertIn("124", combined)
        # Remediation must name the override, not misdirect to shortening the checkout.
        self.assertIn("UV_PROJECT_ENVIRONMENT", combined)

    def test_guard_cli_passes_short_project_environment(self) -> None:
        # The bootable side end-to-end: a short default .venv under `uv run`
        # must pass. Fails (not skips) if uv is present but the step breaks.
        base = self._short_base()
        if base is None:
            self.skipTest("no short temp base available")
        backend = Path(os.path.realpath(str(base))) / "be"
        backend.mkdir()
        (backend / "pyproject.toml").write_text(
            "[project]\n" 'name = "be"\n' 'version = "0.0.1"\n' 'requires-python = ">=3.8"\n',
            encoding="utf-8",
        )
        synced = subprocess.run(["uv", "sync"], cwd=str(backend), capture_output=True, text=True)
        self.assertEqual(synced.returncode, 0, f"uv sync failed with uv present: {synced.stderr}")
        result = subprocess.run(
            ["uv", "run", "--project", str(backend), "python", str(GUARD_PY), "--backend-dir", str(backend)],
            cwd=str(backend),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"guard must pass a short default .venv: {result.stdout}{result.stderr}")


def _strip_shell_comments(text: str) -> str:
    """Blank out ``#`` comments and here-doc-free comment lines for ordering.

    Only line-leading and inline ``#`` comments are removed; this is enough to
    keep explanatory comments that mention mutators from fooling the ordering
    scan, without a full shell parser.
    """
    out_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out_lines.append("")
            continue
        # Drop trailing inline comments (naive but adequate for these scripts).
        out_lines.append(re.sub(r"\s#.*$", "", line))
    return "\n".join(out_lines)


def _strip_powershell_comments(text: str) -> str:
    """Blank out PowerShell comments for the ordering scan.

    PowerShell has both ``#`` line comments and ``<# ... #>`` block comments
    (comment-based help). Help text can mention ``agentcore launch`` and would
    otherwise fool the ordering scan, so the block form is removed first, then
    the ``#`` line/inline form.
    """
    without_blocks = re.sub(r"<#.*?#>", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    return _strip_shell_comments(without_blocks)


_strip_comments = _strip_powershell_comments


class ShellPreflightSkipBehavior(unittest.TestCase):
    """The deploy.sh success echo must be conditional on the guard measuring and
    passing. A native-Windows skip (the guard's dedicated SKIPPED exit code)
    must print the skip line, not the "portable" success line, and must NOT
    abort the deploy under ``set -e``/``pipefail``. A real guard failure must
    still abort.

    This runs the extracted guard block from deploy.sh with ``uv`` stubbed to a
    chosen exit code, so it exercises the actual control flow (not a text scan).
    """

    def _extract_guard_block(self) -> str:
        text = DEPLOY_SH.read_text(encoding="utf-8")
        start = text.find('echo "🔎 Preflight: Verifying the runtime interpreter path is bootable..."')
        self.assertNotEqual(start, -1, "deploy.sh must announce the preflight")
        # The block ends at the download-KB step marker that follows the guard.
        end = text.find("# Step 0: Download KB documentation", start)
        self.assertNotEqual(end, -1, "deploy.sh must have a Step 0 after the guard")
        return text[start:end]

    def _run_block(self, guard_exit: int, guard_stdout: str) -> subprocess.CompletedProcess:
        block = self._extract_guard_block()
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = Path(tmp) / "scripts"
            (script_dir / "infrastructure").mkdir(parents=True)
            (Path(tmp) / "backend").mkdir()
            # A guard placeholder so the referenced path exists.
            (script_dir / "infrastructure" / "check_runtime_path.py").write_text("", encoding="utf-8")
            harness = (
                "set -e\n"
                "set -o pipefail\n"
                f"PROJECT_ROOT={str(tmp)!r}\n"
                f"SCRIPT_DIR={str(script_dir)!r}\n"
                # Stub `uv run python <guard> ...` to emit the chosen stdout and exit code.
                "uv() {\n"
                f"  printf '%s' {guard_stdout!r}\n"
                f"  return {guard_exit}\n"
                "}\n" + block
            )
            return subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_measured_pass_prints_portable_and_continues(self) -> None:
        result = self._run_block(guard_exit=0, guard_stdout="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portable", result.stdout.lower())

    def test_native_windows_skip_prints_skip_not_portable_and_continues(self) -> None:
        # The guard's SKIPPED exit code must not abort the deploy and must not
        # print the "portable" success line; the skip is reported instead.
        result = self._run_block(
            guard_exit=guard.EXIT_SKIPPED_NATIVE_WINDOWS,
            guard_stdout=guard.SKIPPED_NATIVE_WINDOWS_TOKEN
            + " Runtime interpreter path not checked on native Windows (see #610)\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("portable", result.stdout.lower())
        self.assertIn("native Windows", result.stdout)

    def test_guard_failure_aborts_the_deploy(self) -> None:
        # A real unbootable-path failure (exit 1) must abort under set -e and
        # never print the "portable" success line.
        result = self._run_block(guard_exit=1, guard_stdout="unbootable\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("portable", result.stdout.lower())


class PreflightOrdering(unittest.TestCase):
    """The guard must run after local setup and before the first AWS mutation."""

    # First real AWS-mutating command in each deploy path. These are the
    # commands that create cloud side effects; the guard must precede all of
    # them. `sts get-caller-identity` is read-only and intentionally excluded.
    SHELL_MUTATOR = re.compile(r"aws\s+cloudformation\s+deploy")
    SHELL_LAUNCH = re.compile(r"uv\s+run\s+agentcore\s+launch")
    PS_MUTATOR = re.compile(r"Deploy-Stack\b")
    PS_LAUNCH = re.compile(r"uv\s+run\s+agentcore\s+launch")

    def test_shell_guard_runs_before_first_aws_mutation(self) -> None:
        text = _strip_shell_comments(DEPLOY_SH.read_text(encoding="utf-8"))
        guard_idx = text.find("check_runtime_path.py")
        self.assertNotEqual(guard_idx, -1, "deploy.sh must invoke the shared guard")

        mutator = self.SHELL_MUTATOR.search(text)
        self.assertIsNotNone(mutator, "deploy.sh must contain an aws cloudformation deploy")
        self.assertLess(
            guard_idx,
            mutator.start(),
            "guard must run before the first `aws cloudformation deploy`",
        )

        launch = self.SHELL_LAUNCH.search(text)
        self.assertIsNotNone(launch)
        self.assertLess(guard_idx, launch.start(), "guard must precede agentcore launch")

    def test_shell_guard_runs_after_local_uv_sync(self) -> None:
        # The guard measures the real venv, so a `uv sync` must precede it.
        text = _strip_shell_comments(DEPLOY_SH.read_text(encoding="utf-8"))
        guard_idx = text.find("check_runtime_path.py")
        sync_idx = text.find("uv sync")
        self.assertNotEqual(sync_idx, -1, "deploy.sh must sync the venv")
        self.assertLess(sync_idx, guard_idx, "venv must be synced before the guard runs")

    def test_shell_guard_runs_from_physical_backend_directory(self) -> None:
        # The launch resolves the project from the physical getcwd(); the guard
        # must run from the same physical backend directory so it measures the
        # interpreter path that actually ships (not a symlinked spelling). Assert
        # the full invocation shape, not just `pwd -P`: a
        # `uv run --project "$PROJECT_ROOT/backend"` form keeps `pwd -P` yet
        # resolves the project from the logical path, falsely passing a symlinked
        # 125-byte checkout.
        text = _strip_shell_comments(DEPLOY_SH.read_text(encoding="utf-8"))
        guard_idx = text.find("check_runtime_path.py")
        self.assertNotEqual(guard_idx, -1, "deploy.sh must invoke the shared guard")
        # Find the `uv run ... check_runtime_path.py` invocation window.
        window_start = text.rfind("uv run", 0, guard_idx)
        self.assertNotEqual(window_start, -1, "guard must be invoked via `uv run`")
        invocation = text[window_start:guard_idx]
        # The guard must run with a bare `uv run python` (no --project), so uv
        # discovers the project from the physical working directory.
        self.assertNotRegex(
            invocation,
            r"--project",
            "deploy.sh guard must not pin a logical --project; run from the physical cwd",
        )
        self.assertRegex(
            text,
            r"cd\s+\"\$PROJECT_ROOT/backend\"",
            "deploy.sh guard subshell must cd into the backend directory",
        )
        self.assertRegex(text, r"pwd -P", "deploy.sh guard must resolve the physical backend directory")
        self.assertRegex(
            text,
            r"uv run python",
            "deploy.sh guard must invoke the shared guard with a bare `uv run python`",
        )

    def test_shell_no_aws_mutation_precedes_guard(self) -> None:
        # Nothing that mutates AWS may appear before the guard. Scan every
        # `aws <svc> <verb>` occurrence and require it to be read-only if it is
        # positioned before the guard.
        text = _strip_shell_comments(DEPLOY_SH.read_text(encoding="utf-8"))
        guard_idx = text.find("check_runtime_path.py")
        read_only_verbs = {"describe-stacks", "get-caller-identity", "get-login-password"}
        for match in re.finditer(r"aws\s+([a-z0-9-]+)\s+([a-z0-9-]+)", text):
            if match.start() >= guard_idx:
                continue
            verb = match.group(2)
            self.assertIn(
                verb,
                read_only_verbs,
                f"AWS command `aws {match.group(1)} {verb}` runs before the guard",
            )

    def test_powershell_guard_runs_before_first_stack_deploy(self) -> None:
        text = _strip_comments(DEPLOY_PS1.read_text(encoding="utf-8"))
        guard_idx = text.find("Test-GameAgentRuntimePath")
        self.assertNotEqual(guard_idx, -1, "Deploy-GameAgent.ps1 must call the guard")

        # The first Deploy-Stack *invocation* is the first AWS mutation. The
        # function definition `function Deploy-Stack` precedes it but is inert;
        # skip it by searching past the definition.
        def_idx = text.find("function Deploy-Stack")
        mutator = self.PS_MUTATOR.search(text, def_idx + len("function Deploy-Stack"))
        self.assertIsNotNone(mutator, "PowerShell deploy must invoke Deploy-Stack")
        self.assertLess(
            guard_idx,
            mutator.start(),
            "guard must run before the first Deploy-Stack invocation",
        )

        launch = self.PS_LAUNCH.search(text)
        self.assertIsNotNone(launch)
        self.assertLess(guard_idx, launch.start(), "guard must precede agentcore launch")

    def test_powershell_guard_runs_after_local_uv_sync(self) -> None:
        text = _strip_comments(DEPLOY_PS1.read_text(encoding="utf-8"))
        guard_idx = text.find("Test-GameAgentRuntimePath")
        sync_idx = text.find("uv sync")
        self.assertNotEqual(sync_idx, -1, "PowerShell deploy must sync the venv")
        self.assertLess(sync_idx, guard_idx, "venv must be synced before the guard runs")

    def test_powershell_guard_delegates_to_shared_python_implementation(self) -> None:
        # The PowerShell guard must call the shared Python guard, not
        # reimplement the predicate (a PowerShell copy could drift, for example
        # by hard-coding 127 or counting characters).
        guard_text = GUARD_PS1.read_text(encoding="utf-8")
        self.assertIn("check_runtime_path.py", guard_text)
        self.assertNotRegex(
            guard_text,
            r"\b127\b",
            "PowerShell guard must not hard-code the 127 char limit; delegate to the shared guard",
        )

    def test_powershell_default_runner_runs_from_physical_backend_directory(self) -> None:
        # The PowerShell guard's selection correctness depends on running the
        # shared Python guard INSIDE the uv-selected project environment, from
        # the backend directory the launch resolves the project from, so uv's
        # child process discovers the same environment and the guard measures the
        # real <env>/bin/python3 (honoring UV_PROJECT_ENVIRONMENT) rather than a
        # reconstructed path. This pins the same environment selection on the
        # PowerShell deploy path. Fails if the runner is changed to a bare
        # `python` or direct-path invocation that bypasses the selected env, or
        # stops running from inside the backend directory.
        guard_text = GUARD_PS1.read_text(encoding="utf-8")
        self.assertRegex(
            guard_text,
            r"Push-Location\s+\$Backend",
            "default PowerShell GuardRunner must run from inside the backend directory",
        )
        self.assertRegex(
            guard_text,
            r"\$PWD\.ProviderPath",
            "default PowerShell GuardRunner must pass the backend location to the shared guard",
        )
        self.assertRegex(
            guard_text,
            r"uv\s+run\s+python\s+\$Script",
            "default PowerShell GuardRunner must run the shared guard via `uv run`",
        )

    def test_powershell_whatif_short_circuits_before_mutation(self) -> None:
        # -WhatIf returns before the preflight and any mutation.
        text = DEPLOY_PS1.read_text(encoding="utf-8")
        whatif_idx = text.find("ShouldProcess")
        guard_idx = text.find("Test-GameAgentRuntimePath")
        self.assertNotEqual(whatif_idx, -1)
        self.assertLess(whatif_idx, guard_idx, "WhatIf gate must precede the preflight")


if __name__ == "__main__":
    unittest.main()
