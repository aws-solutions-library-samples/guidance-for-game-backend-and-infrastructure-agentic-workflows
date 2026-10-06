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

* ``PreflightOrdering`` proves the guard runs AFTER local dependency setup but
  BEFORE the first AWS-mutating command in both ``deploy.sh`` and
  ``Deploy-GameAgent.ps1`` — the ordering whose absence was the headline defect
  of the first attempt (the guard ran only before ``agentcore launch``, long
  after the first ``aws cloudformation deploy``). The check is red-green: it
  fails if the guard is moved after any mutator or removed.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
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

    def test_windows_launcher_recognized_from_posix_host(self) -> None:
        # A POSIX test host probing a Windows .exe path must still treat it as a
        # launcher target (not reject it with the POSIX byte rule).
        path = "C:\\" + "a" * 300 + "\\Scripts\\python.exe"
        self.assertTrue(guard.venv_python_is_bootable(path, os_name="posix"))

    def test_windows_space_in_launcher_is_bootable(self) -> None:
        # Spaces are fatal only for the POSIX shebang; the Windows launcher
        # binary handles them, so "C:\\Program Files\\..." must not be rejected.
        path = "C:\\Program Files\\game\\.venv\\Scripts\\python.exe"
        self.assertTrue(guard.venv_python_is_bootable(path, os_name="nt"))

    def test_resolved_venv_interpreter_is_absolute(self) -> None:
        resolved = guard.resolve_venv_interpreter(REPOSITORY_ROOT / "backend")
        self.assertTrue(Path(resolved).is_absolute())
        self.assertIn(".venv", resolved)


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

    def test_powershell_whatif_short_circuits_before_mutation(self) -> None:
        # -WhatIf returns before the preflight and any mutation.
        text = DEPLOY_PS1.read_text(encoding="utf-8")
        whatif_idx = text.find("ShouldProcess")
        guard_idx = text.find("Test-GameAgentRuntimePath")
        self.assertNotEqual(whatif_idx, -1)
        self.assertLess(whatif_idx, guard_idx, "WhatIf gate must precede the preflight")


if __name__ == "__main__":
    unittest.main()
