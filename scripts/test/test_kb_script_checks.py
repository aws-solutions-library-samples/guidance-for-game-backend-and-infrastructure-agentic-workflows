"""Static checks for scripts/infrastructure/test-kb.sh.

The KB smoke-test script must target the three per-domain Knowledge Base
stacks (game-agent-kb-gamelift, game-agent-kb-eks, game-agent-kb-cost) and
must never regress to the legacy single-KB stack (see issue #326).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "infrastructure" / "test-kb.sh"

DOMAINS = ("gamelift", "eks", "cost")


class KbScriptStaticChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_script_has_valid_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "bash is required to syntax-check the script")

        result = subprocess.run(
            [bash, "-n", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_is_executable(self) -> None:
        self.assertTrue(SCRIPT_PATH.stat().st_mode & 0o111, "script must be executable")

    def test_targets_all_per_domain_stacks(self) -> None:
        self.assertIn('DOMAINS=(gamelift eks cost)', self.script_text)
        self.assertIn('${PROJECT_NAME}-kb-${domain}', self.script_text)

    def test_does_not_target_legacy_single_kb_stack(self) -> None:
        # The legacy single-KB architecture used a stack named
        # game-agent-knowledge-base; the script must not reference it.
        self.assertNotIn("${PROJECT_NAME}-knowledge-base", self.script_text)
        self.assertNotIn("game-agent-knowledge-base", self.script_text)

    def test_every_domain_has_a_query(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                match = re.search(
                    rf'^\s*{domain}\)\s+echo "([^"]+)"', self.script_text, re.MULTILINE
                )
                self.assertIsNotNone(match, f"no retrieval query mapped for {domain}")
                self.assertTrue(match.group(1).strip(), f"empty query for {domain}")

    def test_queries_are_safe_to_embed_in_json(self) -> None:
        # Queries are interpolated into the JSON --retrieval-query payload, so
        # they must not contain double quotes or backslashes.
        queries = re.findall(r'^\s*(?:gamelift|eks|cost)\)\s+echo "([^"]*)"', self.script_text, re.MULTILINE)
        self.assertEqual(len(queries), len(DOMAINS))
        for query in queries:
            with self.subTest(query=query):
                self.assertNotIn("\\", query)

    def test_fails_loudly_instead_of_soft_passing(self) -> None:
        self.assertIn("exit 1", self.script_text)
        self.assertIn("FAILURES", self.script_text)


if __name__ == "__main__":
    unittest.main()
