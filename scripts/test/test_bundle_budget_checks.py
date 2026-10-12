"""Behavioral contract for the parallel-shell bundle-size budget.

The budget script enforces that no current-UI page carries Cloudscape code or
global CSS and that no current-UI page's first-load grows more than 2 KiB gzip
over its recorded baseline. These tests assert the script and its recorded
baseline exist and are well-formed, run the budget logic against synthetic
builds so the failure paths are tested without a full `next build`, and — when a
real production build is present — run the live budget check and require it to
pass.
"""

# Standard library
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
UI_DIR = PROJECT_ROOT / "ui"
SCRIPT_PATH = UI_DIR / "scripts" / "measure-bundles.mjs"
BASELINE_PATH = UI_DIR / "scripts" / "bundle-baseline.json"
BUILD_MANIFEST = UI_DIR / ".next" / "build-manifest.json"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_against_fixture(next_dir: Path, baseline: dict) -> subprocess.CompletedProcess:
    """Run measure-bundles.mjs against a synthetic .next and baseline."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(baseline, handle)
        baseline_path = handle.name
    return subprocess.run(
        ["node", str(SCRIPT_PATH)],
        cwd=UI_DIR,
        capture_output=True,
        text=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "BUNDLE_NEXT_DIR": str(next_dir),
            "BUNDLE_BASELINE_PATH": baseline_path,
        },
    )


def _build_synthetic_next(tmp: Path, manifest: dict, files: dict) -> Path:
    """Create a synthetic .next directory with a build-manifest and chunk files."""
    next_dir = tmp / ".next"
    _write(next_dir / "build-manifest.json", json.dumps(manifest))
    for rel_path, content in files.items():
        _write(next_dir / rel_path, content)
    return next_dir


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

    def test_clean_build_exits_zero(self):
        """A current-UI page within its baseline and with no Cloudscape passes."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            manifest = {"pages": {"/": ["static/clean.js"], "/_app": ["static/app.js"]}}
            files = {
                "static/clean.js": "export const page = 1;\n",
                "static/app.js": "export const app = 1;\n",
            }
            next_dir = _build_synthetic_next(tmp, manifest, files)
            baseline = {"pages": {"/": {"jsFirstLoadKiB": 100.0}}}
            result = _run_against_fixture(next_dir, baseline)
            self.assertEqual(result.returncode, 0, f"clean build should pass:\n{result.stdout}\n{result.stderr}")

    def test_cloudscape_chunk_on_current_page_exits_one(self):
        """A Cloudscape chunk on a current-UI page must fail the budget."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            manifest = {"pages": {"/": ["static/with-cloudscape.js"], "/_app": ["static/app.js"]}}
            files = {
                # The script greps file CONTENT for the Cloudscape marker.
                "static/with-cloudscape.js": "import '@cloudscape-design/global-styles';\n",
                "static/app.js": "export const app = 1;\n",
            }
            next_dir = _build_synthetic_next(tmp, manifest, files)
            baseline = {"pages": {"/": {"jsFirstLoadKiB": 100.0}}}
            result = _run_against_fixture(next_dir, baseline)
            self.assertEqual(result.returncode, 1, f"Cloudscape on a current page must fail:\n{result.stdout}\n{result.stderr}")
            self.assertIn("Cloudscape", result.stderr)

    def test_growth_over_ceiling_exits_one(self):
        """A current-UI page growing more than 2 KiB over baseline must fail."""
        import os

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            # Random (incompressible) payload so the gzip size stays large and
            # well above the +2 KiB ceiling relative to the tiny baseline.
            big = os.urandom(8192).hex()  # ~16 KiB of high-entropy text
            manifest = {"pages": {"/": ["static/big.js"], "/_app": ["static/app.js"]}}
            files = {"static/big.js": big, "static/app.js": "export const app = 1;\n"}
            next_dir = _build_synthetic_next(tmp, manifest, files)
            # Baseline well below the fixture's measured size so growth > 2.1 KiB.
            baseline = {"pages": {"/": {"jsFirstLoadKiB": 0.1}}}
            result = _run_against_fixture(next_dir, baseline)
            self.assertEqual(result.returncode, 1, f"growth over ceiling must fail:\n{result.stdout}\n{result.stderr}")
            self.assertIn("grew", result.stderr)

    def test_error_page_is_scanned(self):
        """/_error serves the current UI, so a Cloudscape import there must fail."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            manifest = {
                "pages": {
                    "/": ["static/clean.js"],
                    "/_error": ["static/error-cloudscape.js"],
                    "/_app": ["static/app.js"],
                }
            }
            files = {
                "static/clean.js": "export const page = 1;\n",
                "static/error-cloudscape.js": "import '@cloudscape-design/components';\n",
                "static/app.js": "export const app = 1;\n",
            }
            next_dir = _build_synthetic_next(tmp, manifest, files)
            baseline = {"pages": {"/": {"jsFirstLoadKiB": 100.0}}}
            result = _run_against_fixture(next_dir, baseline)
            self.assertEqual(result.returncode, 1, f"Cloudscape on /_error must fail:\n{result.stdout}\n{result.stderr}")
            self.assertIn("/_error", result.stderr)

    def test_cloudscape_shell_page_is_allowed(self):
        """The Cloudscape shell entry is expected to carry Cloudscape and passes."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            manifest = {
                "pages": {
                    "/shell-cloudscape": ["static/shell-cloudscape.js"],
                    "/_app": ["static/app.js"],
                }
            }
            files = {
                "static/shell-cloudscape.js": "import '@cloudscape-design/components/app-layout';\n",
                "static/app.js": "export const app = 1;\n",
            }
            next_dir = _build_synthetic_next(tmp, manifest, files)
            baseline = {"pages": {}}
            result = _run_against_fixture(next_dir, baseline)
            self.assertEqual(result.returncode, 0, f"Cloudscape shell page should pass:\n{result.stdout}\n{result.stderr}")

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
