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

* ``UvGeneratedEntrypointBoundary`` proves the guard against uv's REAL generated
  console-script artifact: it builds a minimal local wheel (no network),
  installs it with uv into venvs whose interpreter path we control to 124 bytes,
  125 bytes, and a short space-bearing path, reads the first line uv actually
  wrote, and requires the guard verdict to agree with uv's shebang/trampoline
  choice. It also runs the guard via ``uv run --project`` against a long external
  ``UV_PROJECT_ENVIRONMENT`` to prove end-to-end selection + measurement. This is
  the artifact boundary that caused #517, which the first iteration only asserted
  about.

* ``RemediationMessage`` proves the failure text matches where the unbootable
  interpreter came from — shorten-the-checkout for a default ``.venv`` path, but
  unset/relocate ``UV_PROJECT_ENVIRONMENT`` for an external uv-selected env.

* ``PreflightOrdering`` proves the guard runs AFTER local dependency setup but
  BEFORE the first AWS-mutating command in both ``deploy.sh`` and
  ``Deploy-GameAgent.ps1`` — the ordering whose absence was the headline defect
  of the first attempt (the guard ran only before ``agentcore launch``, long
  after the first ``aws cloudformation deploy``). The check is red-green: it
  fails if the guard is moved after any mutator or removed.
"""

from __future__ import annotations

# Standard library
import importlib.util
import os
import re
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
        # Root-cause regression for the first rejected fix: a POSIX host NEVER
        # emits a Windows launcher. uv's gate is `os_name == "posix"` only, so a
        # long `.exe` interpreter path on POSIX still gets the unbootable
        # `/bin/sh` trampoline. The guard must NOT treat the `.exe` suffix as a
        # launcher and wrongly pass it. RED against the removed suffix heuristic.
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


class RuntimeInterpreterSelection(unittest.TestCase):
    """The guard must measure the interpreter uv actually selected, not a
    reconstructed ``<backend>/.venv`` path.

    uv 0.12.0 honors ``UV_PROJECT_ENVIRONMENT``, so the real project environment
    (the one whose interpreter path ``agentcore launch`` embeds) can live outside
    ``<backend>/.venv``. Both deploy paths run this guard via
    ``uv run --project <backend>``, which executes it *inside* that selected
    environment; the running ``sys.executable`` is therefore the authoritative
    path. These tests pin that selection by controlling ``sys.executable`` and
    ``VIRTUAL_ENV`` exactly as ``uv run`` sets them -- no uv or real venv needed.
    """

    def setUp(self) -> None:
        self._orig_executable = guard.sys.executable
        self._orig_virtual_env = os.environ.get("VIRTUAL_ENV")

    def tearDown(self) -> None:
        guard.sys.executable = self._orig_executable
        if self._orig_virtual_env is None:
            os.environ.pop("VIRTUAL_ENV", None)
        else:
            os.environ["VIRTUAL_ENV"] = self._orig_virtual_env

    def _simulate_uv_run(self, env_dir: str) -> None:
        """Mimic ``uv run``: VIRTUAL_ENV is the env, sys.executable is inside it."""
        os.environ["VIRTUAL_ENV"] = env_dir
        guard.sys.executable = str(Path(env_dir) / "bin" / "python3")

    def test_long_project_environment_overrides_short_backend_venv(self) -> None:
        # The release-path mismatch: a SHORT backend/.venv path (would pass the
        # old reconstructing guard) while uv actually selected a LONG project
        # environment via UV_PROJECT_ENVIRONMENT. The guard must measure the
        # real selected interpreter and REJECT it. RED against the old
        # resolve_venv_interpreter-only guard; GREEN once sys.executable wins.
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

    def test_falls_back_to_backend_venv_outside_project_environment(self) -> None:
        # A bare system python3 (no VIRTUAL_ENV, executable not under
        # backend/.venv) is not trusted to represent the runtime, so the guard
        # falls back to the deterministic backend/.venv reconstruction.
        os.environ.pop("VIRTUAL_ENV", None)
        guard.sys.executable = "/usr/bin/python3"
        backend = REPOSITORY_ROOT / "backend"
        resolved = guard.resolve_runtime_interpreter(backend)
        self.assertEqual(resolved, guard.resolve_venv_interpreter(backend))

    def test_default_venv_layout_is_trusted_without_virtual_env(self) -> None:
        # A direct `<backend>/.venv/bin/python3 check_runtime_path.py` call sets
        # no VIRTUAL_ENV but still runs the real runtime interpreter; the guard
        # must trust it via the default-layout check and measure sys.executable.
        os.environ.pop("VIRTUAL_ENV", None)
        backend = Path("/srv/app/backend")
        guard.sys.executable = str(backend / ".venv" / "bin" / "python3")
        resolved = guard.resolve_runtime_interpreter(backend)
        self.assertEqual(resolved, os.path.abspath(guard.sys.executable))


class RemediationMessage(unittest.TestCase):
    """Remediation must match where the unbootable interpreter came from.

    A path under the default ``<backend>/.venv`` is repaired by a shorter
    checkout. An interpreter uv selected via ``UV_PROJECT_ENVIRONMENT`` (outside
    ``<backend>/.venv``) cannot be fixed by moving the checkout, so the message
    must point at the override instead -- the first fix always recommended
    shortening the checkout and the literal ``backend/.venv/bin/python3`` path,
    which is wrong for the external-environment case the resolver now detects.
    """

    def test_default_venv_path_advises_shortening_checkout(self) -> None:
        backend = Path("/srv/app/backend")
        interp = str(backend / ".venv" / "bin" / ("p" * 200))
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("shorter", msg.lower())
        self.assertNotIn("UV_PROJECT_ENVIRONMENT", msg)

    def test_external_environment_advises_unsetting_override(self) -> None:
        # The interpreter uv selected lives outside backend/.venv -> override.
        backend = Path("/srv/app/backend")
        interp = "/" + "e" * 200 + "/bin/python3"
        msg = guard.remediation_message(interp, backend_dir=backend)
        self.assertIn("UV_PROJECT_ENVIRONMENT", msg)
        # Must not misdirect the user to shorten the checkout for an override.
        self.assertNotRegex(msg.lower(), r"deploy from a checkout")

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

    def _run_default_path(self, *, virtual_env: str, executable_env: str) -> subprocess.CompletedProcess:
        """Invoke the CLI with NO ``--interpreter`` so it exercises the default
        ``resolve_runtime_interpreter`` path, with ``VIRTUAL_ENV`` and a
        bootstrap interpreter chosen to mimic ``uv run`` selecting ``executable_env``.
        """
        # A tiny bootstrap re-execs the guard's main() after pointing
        # sys.executable at the simulated uv-selected environment interpreter,
        # exactly as `uv run` would present it to the guard process.
        bootstrap = (
            "import os, sys, runpy;"
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
        # exact release-path mismatch the previous iteration missed.
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
class UvGeneratedEntrypointBoundary(unittest.TestCase):
    """Prove the guard against uv's REAL generated console-script artifact.

    This is the boundary that caused #517 and that the first iteration's tests
    only *asserted* about: ``agentcore launch`` packages the venv's uv-generated
    console scripts, and uv's ``format_shebang`` decides at install time whether
    each script is a portable ``#!`` shebang or an unbootable ``/bin/sh``
    trampoline. These tests build a minimal local wheel (no network), install it
    with uv into venvs whose interpreter path length/content we control, read
    the FIRST LINE uv actually wrote, and require the guard's verdict to agree
    with uv's artifact. If uv's rule ever shifts, these go red -- they are not a
    restatement of the guard's own arithmetic.

    Grounded in uv source: ``crates/uv-install-wheel/src/wheel.rs``
    ``format_shebang`` (identical rule across 0.10.x and 0.12.x): on POSIX the
    ``/bin/sh`` trampoline fires when ``2 + executable.len() + 1 > 127`` (path
    over 124 bytes) or the path contains a space.
    """

    _tmp: tempfile.TemporaryDirectory[str]
    _wheel: Path
    _root: Path

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
        if proc.returncode != 0:
            raise unittest.SkipTest(f"uv build failed, cannot exercise artifact: {proc.stderr}")
        wheels = list(dist.glob("*.whl"))
        if not wheels:
            raise unittest.SkipTest("uv build produced no wheel")
        cls._wheel = wheels[0]
        cls._root = root

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _make_venv_with_interpreter_bytes(self, target_bytes: int) -> tuple[str, str] | None:
        """Create a uv venv whose POSIX ``bin/python3`` path is ``target_bytes``.

        Returns ``(interpreter_path, first_line_of_console_script)`` or ``None``
        when the system temp base is already too long to hit ``target_bytes``.
        """
        base = Path(tempfile.mkdtemp(prefix="e", dir=self._root))
        tail = "/bin/python3"
        # path = base + "/" + name + tail
        name_len = target_bytes - len(os.fsencode(str(base))) - 1 - len(tail)
        if name_len < 1:
            return None
        venv_dir = base / ("v" * name_len)
        interpreter = str(venv_dir / "bin" / "python3")
        if len(os.fsencode(interpreter)) != target_bytes:
            return None
        return self._install_and_read(venv_dir, interpreter)

    def _install_and_read(self, venv_dir: Path, interpreter: str) -> tuple[str, str] | None:
        created = subprocess.run(
            ["uv", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            return None
        installed = subprocess.run(
            ["uv", "pip", "install", "--python", interpreter, str(self._wheel)],
            capture_output=True,
            text=True,
            env=dict(os.environ, VIRTUAL_ENV=str(venv_dir)),
        )
        if installed.returncode != 0:
            return None
        script = venv_dir / "bin" / "sampletool"
        if not script.exists():
            return None
        first_line = script.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        return interpreter, first_line

    def _is_portable_shebang(self, first_line: str, interpreter: str) -> bool:
        # uv emits `#!<interpreter>` for a portable shebang, or `#!/bin/sh` for
        # the trampoline. Distinguish by whether the real interpreter path is on
        # the shebang line (the trampoline uses /bin/sh instead).
        return first_line.startswith("#!") and interpreter in first_line

    def test_guard_agrees_with_uv_at_124_bytes(self) -> None:
        made = self._make_venv_with_interpreter_bytes(124)
        if made is None:
            self.skipTest("temp base too long to construct a 124-byte interpreter path")
        interpreter, first_line = made
        uv_portable = self._is_portable_shebang(first_line, interpreter)
        self.assertTrue(uv_portable, f"expected uv to emit a portable shebang at 124 bytes: {first_line!r}")
        self.assertEqual(
            guard.venv_python_is_bootable(interpreter, os_name="posix"),
            uv_portable,
            "guard verdict must match uv's generated console script at 124 bytes",
        )

    def test_guard_agrees_with_uv_at_125_bytes(self) -> None:
        made = self._make_venv_with_interpreter_bytes(125)
        if made is None:
            self.skipTest("temp base too long to construct a 125-byte interpreter path")
        interpreter, first_line = made
        uv_portable = self._is_portable_shebang(first_line, interpreter)
        self.assertFalse(uv_portable, f"expected uv to emit a /bin/sh trampoline at 125 bytes: {first_line!r}")
        self.assertEqual(
            guard.venv_python_is_bootable(interpreter, os_name="posix"),
            uv_portable,
            "guard verdict must match uv's generated console script at 125 bytes",
        )

    def test_guard_agrees_with_uv_for_space_bearing_short_path(self) -> None:
        venv_dir = Path(tempfile.mkdtemp(prefix="s", dir=self._root)) / "My Env"
        interpreter = str(venv_dir / "bin" / "python3")
        made = self._install_and_read(venv_dir, interpreter)
        if made is None:
            self.skipTest("uv could not create the space-bearing venv")
        interpreter, first_line = made
        self.assertLessEqual(
            guard.interpreter_path_bytes(interpreter),
            INTERPRETER_BYTE_LIMIT,
            "precondition: the space-bearing path is short enough that only the space can trip uv",
        )
        uv_portable = self._is_portable_shebang(first_line, interpreter)
        self.assertFalse(uv_portable, f"expected uv to trampoline on the space: {first_line!r}")
        self.assertFalse(
            guard.venv_python_is_bootable(interpreter, os_name="posix"),
            "guard must reject a space-bearing interpreter path uv trampolines",
        )

    def test_guard_cli_rejects_real_long_external_project_environment(self) -> None:
        # End-to-end on the release path: a SHORT backend dir with a LONG
        # external UV_PROJECT_ENVIRONMENT. The guard is run exactly as the
        # deploy scripts run it -- `uv run --project <backend>` -- so it executes
        # inside the uv-selected environment and measures the real
        # `sys.executable` uv embeds into the console scripts. uv selects the
        # long external env; the guard must fail. This is the mismatch the first
        # iteration missed, proven against uv's own environment selection.
        backend = Path(tempfile.mkdtemp(prefix="be", dir=self._root)) / "backend"
        backend.mkdir()
        (backend / "pyproject.toml").write_text(
            "[project]\n" 'name = "be"\n' 'version = "0.0.1"\n' 'requires-python = ">=3.8"\n',
            encoding="utf-8",
        )
        ext_base = Path(tempfile.mkdtemp(prefix="ext", dir=self._root))
        # Pad the external env name so the selected interpreter is well over 124
        # bytes regardless of the temp base length.
        long_env = ext_base / ("e" * 160)
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


def _strip_shell_comments(text: str) -> str:
    """Blank out ``#`` comments and here-doc-free comment lines for ordering.

    Only line-leading and inline ``#`` comments are removed; this is enough to
    keep explanatory comments that mention mutators from fooling the ordering
    scan, without a full shell parser. PowerShell uses the same ``#`` comment
    syntax, so this serves both deploy paths.
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


_strip_comments = _strip_shell_comments


class PreflightOrdering(unittest.TestCase):
    """The guard must run after local setup and before the first AWS mutation."""

    # First real AWS-mutating command in each deploy path. These are the
    # commands that create cloud side effects; the guard must precede all of
    # them. `sts get-caller-identity` is read-only and intentionally excluded.
    SHELL_MUTATOR = re.compile(r"aws\s+cloudformation\s+deploy")
    SHELL_LAUNCH = re.compile(r"agentcore\s+launch")
    PS_MUTATOR = re.compile(r"Deploy-Stack\b")
    PS_LAUNCH = re.compile(r"agentcore\s+launch")

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
        # reimplement the predicate (the first attempt hard-coded 127 and a
        # char-length check in PowerShell).
        guard_text = GUARD_PS1.read_text(encoding="utf-8")
        self.assertIn("check_runtime_path.py", guard_text)
        self.assertNotRegex(
            guard_text,
            r"\b127\b",
            "PowerShell guard must not hard-code the 127 char limit; delegate to the shared guard",
        )

    def test_powershell_default_runner_runs_inside_uv_selected_environment(self) -> None:
        # The PowerShell guard's selection correctness depends on running the
        # shared Python guard INSIDE the uv-selected project environment, so the
        # guard measures the real `sys.executable` (honoring
        # UV_PROJECT_ENVIRONMENT) rather than a reconstructed `.venv` path. The
        # default GuardRunner must therefore invoke `uv run --project <backend>`.
        # This pins the same selection fix as the Python-side regression on the
        # PowerShell deploy path. RED if the runner is changed to a bare
        # `python` / direct-path invocation that bypasses the selected env.
        guard_text = GUARD_PS1.read_text(encoding="utf-8")
        self.assertRegex(
            guard_text,
            r"uv\s+run\s+--project\s+\$Backend\s+python\s+\$Script",
            "default PowerShell GuardRunner must run the shared guard via `uv run --project`",
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
