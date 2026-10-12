"""Behavioral contract for the parallel-shell bundle-size budget.

The budget script enforces that no current-UI page carries Cloudscape code or
global CSS and that no current-UI page's first-load grows more than 2 KiB gzip
over its recorded baseline. These tests assert the script and its recorded
baseline exist and are well-formed, and — when a production build is present —
run the live budget check and require it to pass.
"""

# Standard library
import json
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
UI_DIR = PROJECT_ROOT / "ui"
SCRIPT_PATH = UI_DIR / "scripts" / "measure-bundles.mjs"
BASELINE_PATH = UI_DIR / "scripts" / "bundle-baseline.json"
BUILD_MANIFEST = UI_DIR / ".next" / "build-manifest.json"


class BundleBudgetChecks(unittest.TestCase):
    def test_measurement_script_and_baseline_exist(self):
        self.assertTrue(SCRIPT_PATH.is_file(), "ui/scripts/measure-bundles.mjs must exist")
        self.assertTrue(BASELINE_PATH.is_file(), "ui/scripts/bundle-baseline.json must exist")

    def test_baseline_records_current_ui_pages(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertIn("/", baseline["pages"], "home page baseline must be recorded")
        self.assertIsInstance(baseline["pages"]["/"]["jsFirstLoadKiB"], (int, float))

    def test_script_enforces_cloudscape_isolation_and_growth_ceiling(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("awsui", text, "script must detect Cloudscape (awsui) modules")
        self.assertIn("cloudscape-design", text, "script must detect Cloudscape modules")
        self.assertIn("GROWTH_CEILING_KIB", text, "script must enforce a per-page growth ceiling")

    def test_live_budget_passes_when_build_present(self):
        if not BUILD_MANIFEST.is_file():
            self.skipTest("no production build present (.next/build-manifest.json missing)")
        result = subprocess.run(
            ["node", "scripts/measure-bundles.mjs"],
            cwd=UI_DIR,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"bundle budget failed:\n{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
