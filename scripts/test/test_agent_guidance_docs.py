"""Static checks for the consolidated agent guidance file.

`AGENTS.md` is the repository's sole agent instruction file. `CLAUDE.md` was
retired once its unique, still-valid guidance was folded into `AGENTS.md`
(see issue #449). These checks prevent the retired file, or any tracked
reference to it, from returning and drifting the instructions back apart.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENTS_PATH = REPOSITORY_ROOT / "AGENTS.md"
RETIRED_DOC = "CLAUDE.md"
# This regression check names the retired doc on purpose; it is not a stray
# reference that would drift the guidance back apart.
SELF_PATH = Path(__file__).resolve().relative_to(REPOSITORY_ROOT).as_posix()
AGENT_GUIDANCE_FILENAMES = frozenset({"agents.md", "claude.md", "gemini.md"})
AGENT_GUIDANCE_EXACT_PATHS = frozenset({".cursorrules", ".github/copilot-instructions.md"})
AGENT_GUIDANCE_PREFIXES = (".claude/", ".cursor/rules/", ".github/instructions/", ".kiro/steering/")


def _is_agent_guidance_path(path: str) -> bool:
    """Return whether a tracked path is a recognized agent instruction file."""
    normalized_path = path.casefold()
    filename = normalized_path.rsplit("/", maxsplit=1)[-1]
    return (
        filename in AGENT_GUIDANCE_FILENAMES
        or normalized_path in AGENT_GUIDANCE_EXACT_PATHS
        or normalized_path.startswith(AGENT_GUIDANCE_PREFIXES)
    )


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


class AgentGuidanceDocChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tracked = _tracked_files()

    def test_agents_md_exists_and_is_nonempty(self) -> None:
        self.assertTrue(AGENTS_PATH.is_file(), "AGENTS.md must exist at the repository root")
        self.assertTrue(AGENTS_PATH.read_text(encoding="utf-8").strip(), "AGENTS.md must not be empty")

    def test_agents_md_is_the_only_tracked_agent_instruction_file(self) -> None:
        guidance_paths = sorted(path for path in self.tracked if _is_agent_guidance_path(path))
        self.assertEqual(
            guidance_paths,
            ["AGENTS.md"],
            f"AGENTS.md must be the sole tracked agent instruction file: {guidance_paths}",
        )

    def test_retired_claude_doc_is_absent(self) -> None:
        self.assertNotIn(RETIRED_DOC, self.tracked, "CLAUDE.md must remain retired")
        self.assertFalse((REPOSITORY_ROOT / RETIRED_DOC).exists(), "CLAUDE.md must not exist on disk")

    def test_no_tracked_file_references_the_retired_doc(self) -> None:
        result = subprocess.run(
            ["git", "grep", "-l", "-I", "-i", RETIRED_DOC.replace(".", r"\.")],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        # git grep exits 0 when it finds matches and 1 when there are none; any
        # higher exit code is a command failure, not a clean "no references"
        # result, so fail loudly with stderr instead of treating empty stdout as
        # proof that nothing references the retired doc.
        self.assertIn(
            result.returncode,
            (0, 1),
            f"git grep failed (exit {result.returncode}): {result.stderr.strip()}",
        )
        offenders = [line for line in result.stdout.splitlines() if line and line != SELF_PATH]
        self.assertEqual(offenders, [], f"tracked files still reference {RETIRED_DOC}: {offenders}")


if __name__ == "__main__":
    unittest.main()
