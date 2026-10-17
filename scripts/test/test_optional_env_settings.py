"""Regression test: optional ui/.env.local settings must not abort the deploy.

deploy.sh runs with `set -e` and `pipefail`. Reading an optional key with
`$(grep KEY file | ...)` exits non-zero when the key is absent, which silently
ends the deployment. Every optional read must tolerate a missing key.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("scripts/deploy.sh", "scripts/infrastructure/deploy-kb.sh")
OPTIONAL_KEYS = ("GBAW_IAM_ROLE_PATH", "GBAW_IAM_PERMISSIONS_BOUNDARY_ARN", "GBAW_FRONTEND_SUBNET_IDS")


class OptionalEnvSettingTests(unittest.TestCase):
    def reads(self) -> list[tuple[str, str]]:
        found = []
        for script in SCRIPTS:
            for line in (REPOSITORY_ROOT / script).read_text(encoding="utf-8").splitlines():
                if "ui/.env.local" in line and "$(grep" in line and any(
                    key in line or "_iam_setting" in line for key in OPTIONAL_KEYS
                ):
                    found.append((script, line.strip()))
        return found

    def test_every_optional_read_tolerates_a_missing_key(self) -> None:
        reads = self.reads()
        self.assertGreaterEqual(len(reads), 3)
        for script, line in reads:
            with self.subTest(script=script, line=line):
                self.assertRegex(line, r"\|\| true\)$")

    def test_missing_key_read_succeeds_under_set_e_and_pipefail(self) -> None:
        _, line = self.reads()[-1]
        assignment = re.sub(r'"\$PROJECT_ROOT/ui/\.env\.local"', '"$ENV_FILE"', line)
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("AWS_REGION=us-west-2\n")
        script = f'set -euo pipefail\nENV_FILE="{handle.name}"\n_iam_setting=GBAW_IAM_ROLE_PATH\n{assignment}\necho reached'
        try:
            result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10, check=False)
        finally:
            Path(handle.name).unlink()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached", result.stdout)


if __name__ == "__main__":
    unittest.main()
