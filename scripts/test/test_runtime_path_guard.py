"""Static and behavioral checks for the AgentCore runtime path-length guard.

Issue #517: when the backend venv interpreter path
(`$PROJECT_ROOT/backend/.venv/bin/python3`) exceeds the kernel shebang limit of
127 characters, uv emits a `/bin/sh` exec trampoline embedding that absolute
local path instead of a portable `#!/usr/bin/env python3` shebang. The starter
toolkit's shebang rewriter only handles single-line `#!` shebangs, so the
trampoline ships unchanged inside `dependencies.zip/bin/*` and the runtime dies
at exec before any app code runs. The deploy silently exits 0.

The fix is a deploy-time fail-fast guard, shared by `scripts/deploy.sh` (via a
sourceable helper) and mirrored in the PowerShell deployment path. These checks
pin the behavior so the guard cannot silently regress.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPOSITORY_ROOT / "scripts" / "infrastructure" / "check-runtime-path.sh"
DEPLOY_SH = REPOSITORY_ROOT / "scripts" / "deploy.sh"
DEPLOY_PS1 = REPOSITORY_ROOT / "scripts" / "powershell" / "Public" / "Deploy-GameAgent.ps1"

# The kernel shebang limit. A venv interpreter path at or below this length
# yields a portable `#!` shebang; above it, uv falls back to the /bin/sh
# trampoline that cannot boot in the read-only AgentCore container.
SHEBANG_LIMIT = 127


class RuntimePathHelperBehavior(unittest.TestCase):
    """Exercise the real shipped guard logic, not a reimplementation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = shutil.which("bash")
        if cls.bash is None:
            raise unittest.SkipTest("bash is required to exercise the guard helper")
        if not HELPER_PATH.exists():
            raise unittest.SkipTest(f"guard helper not present: {HELPER_PATH}")

    def _run_guard(self, venv_python_path: str) -> subprocess.CompletedProcess:
        # Source the helper and invoke the guard with a synthetic path so the
        # test does not depend on the real checkout location.
        script = (
            f'. "{HELPER_PATH}"\n'
            f'assert_portable_venv_python "{venv_python_path}"\n'
        )
        return subprocess.run(
            [self.bash, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_helper_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            [self.bash, "-n", str(HELPER_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_short_path_passes(self) -> None:
        path = "/tmp/x/backend/.venv/bin/python3"
        self.assertLessEqual(len(path), SHEBANG_LIMIT)
        result = self._run_guard(path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_path_at_limit_passes(self) -> None:
        # Exactly 127 characters still produces a portable shebang.
        path = "/" + "a" * (SHEBANG_LIMIT - 1)
        self.assertEqual(len(path), SHEBANG_LIMIT)
        result = self._run_guard(path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_long_path_fails_fast(self) -> None:
        # 128+ characters is where uv emits the unbootable /bin/sh trampoline.
        path = "/" + "a" * SHEBANG_LIMIT
        self.assertGreater(len(path), SHEBANG_LIMIT)
        result = self._run_guard(path)
        self.assertNotEqual(result.returncode, 0, "guard must fail on an over-limit path")
        combined = (result.stdout + result.stderr)
        # The message must be actionable: name the limit and point at a shorter
        # checkout as the remediation.
        self.assertIn("127", combined)
        self.assertRegex(combined.lower(), r"shorter|path length|exceeds")

    def test_realistic_ci_path_fails(self) -> None:
        # GitHub Actions-style path from the issue triage comment (~190 chars).
        path = (
            "/home/runner/work/"
            "guidance-for-game-backend-and-infrastructure-agentic-workflows/"
            "guidance-for-game-backend-and-infrastructure-agentic-workflows/"
            "backend/.venv/bin/python3"
        )
        self.assertGreater(len(path), SHEBANG_LIMIT)
        result = self._run_guard(path)
        self.assertNotEqual(result.returncode, 0, result.stderr)


class DeployShellGuardWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DEPLOY_SH.read_text(encoding="utf-8")

    def test_sources_the_guard_helper(self) -> None:
        self.assertIn("check-runtime-path.sh", self.text)

    def test_invokes_guard_before_agentcore_launch(self) -> None:
        guard_idx = self.text.find("assert_portable_venv_python")
        launch_idx = self.text.find("agentcore launch")
        self.assertNotEqual(guard_idx, -1, "deploy.sh must call the guard")
        self.assertNotEqual(launch_idx, -1)
        self.assertLess(
            guard_idx,
            launch_idx,
            "guard must run before the first agentcore launch",
        )


class DeployPowerShellGuardWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DEPLOY_PS1.exists():
            raise unittest.SkipTest(f"PowerShell deploy not present: {DEPLOY_PS1}")
        cls.text = DEPLOY_PS1.read_text(encoding="utf-8")

    def test_references_the_limit(self) -> None:
        self.assertIn("127", self.text)

    def test_guard_runs_before_agentcore_launch(self) -> None:
        # The PowerShell guard is identified by the shared marker string.
        guard_idx = self.text.find("venv interpreter path")
        launch_idx = self.text.find("agentcore launch")
        self.assertNotEqual(guard_idx, -1, "PowerShell deploy must guard the venv path")
        self.assertNotEqual(launch_idx, -1)
        self.assertLess(guard_idx, launch_idx, "guard must run before agentcore launch")


if __name__ == "__main__":
    unittest.main()
