"""Regression checks for the local Playwright step in test-local.sh (#742).

The step once selected a tag that no spec used and swallowed Playwright's exit
code, so it ran zero browser tests and always passed.
"""

# Standard library
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
TEST_LOCAL = PROJECT_ROOT / "test-local.sh"
UI_TESTS = PROJECT_ROOT / "ui" / "tests"
MEMORY_SPEC = UI_TESTS / "e2e-memory-behavior.spec.ts"

# Mirrors testIgnore in ui/playwright.config.ts: these specs run under their
# own configs, never in the default suite test-local.sh invokes.
DEFAULT_CONFIG_IGNORED = re.compile(r"(^live-shakedown\.spec\.ts$|\.cloudscape\.spec\.ts$)")
TAG_ARRAY = re.compile(r"tag:\s*\[([^\]]*)\]")
SWALLOWED_EXIT = re.compile(r"\|\|\s*(echo\b|true\b|:)")


def _script_lines(pattern):
    return [line for line in TEST_LOCAL.read_text(encoding="utf-8").splitlines() if pattern in line]


def _spec_tags(spec):
    tags = set()
    for array in TAG_ARRAY.findall(spec.read_text(encoding="utf-8")):
        tags.update(re.findall(r"['\"](@[\w-]+)['\"]", array))
    return tags


class LocalPlaywrightSelectionTests(unittest.TestCase):
    def test_playwright_step_cannot_pass_without_running_tests(self):
        lines = _script_lines("npx playwright test")
        self.assertEqual(len(lines), 1, "test-local.sh should invoke Playwright exactly once")
        line = lines[0]
        self.assertIsNone(SWALLOWED_EXIT.search(line), f"Playwright's exit code must not be swallowed: {line}")
        self.assertNotIn("--pass-with-no-tests", line)
        self.assertIn("set -e", TEST_LOCAL.read_text(encoding="utf-8"))

    def test_jest_step_fails_on_an_empty_selection(self):
        lines = _script_lines("npm test")
        self.assertTrue(lines, "test-local.sh should run the frontend Jest suite")
        for line in lines:
            self.assertNotIn("--passWithNoTests", line)
            self.assertIsNone(SWALLOWED_EXIT.search(line), f"Jest's exit code must not be swallowed: {line}")

    def test_selected_tag_matches_default_suite_specs(self):
        grep = re.search(r"--grep[= ](@[\w-]+)", _script_lines("npx playwright test")[0])
        self.assertIsNotNone(grep, "the Playwright step should select specs with --grep @tag")
        tag = grep.group(1)
        selected = sorted(
            spec.name
            for spec in UI_TESTS.glob("*.spec.ts")
            if not DEFAULT_CONFIG_IGNORED.search(spec.name) and tag in _spec_tags(spec)
        )
        self.assertTrue(selected, f"no default-suite spec carries {tag}, so the step would run zero tests")
        # Specs that need deployed credentials or configured AgentCore Memory
        # must not be part of the fast local selection.
        self.assertNotIn(MEMORY_SPEC.name, selected)
        self.assertNotIn("full-system-test.spec.ts", selected)

    def test_memory_specs_skip_without_configured_memory(self):
        source = MEMORY_SPEC.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"test\.skip\(\s*!process\.env\.BEDROCK_AGENTCORE_MEMORY_ID",
            "memory specs should skip with a reason unless AgentCore Memory is configured",
        )


if __name__ == "__main__":
    unittest.main()
