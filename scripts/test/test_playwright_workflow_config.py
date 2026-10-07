"""Behavioral contract for the Playwright test runner image."""

# Standard library
import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
PACKAGE_LOCK_PATH = PROJECT_ROOT / "ui" / "package-lock.json"
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "test.yml"
PLAYWRIGHT_IMAGE_PATTERN = re.compile(
    r"image:\s*mcr\.microsoft\.com/playwright:v(?P<version>\d+\.\d+\.\d+)-noble"
)


class PlaywrightWorkflowConfigTests(unittest.TestCase):
    def test_runner_image_matches_locked_playwright_version(self):
        package_lock = json.loads(PACKAGE_LOCK_PATH.read_text(encoding="utf-8"))
        locked_version = package_lock["packages"]["node_modules/@playwright/test"]["version"]
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        image_match = PLAYWRIGHT_IMAGE_PATTERN.search(workflow)

        self.assertIsNotNone(image_match, "Playwright CI image must be pinned to a noble version")
        self.assertEqual(image_match.group("version"), locked_version)


if __name__ == "__main__":
    unittest.main()
