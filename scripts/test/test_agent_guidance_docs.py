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
        # git grep exits 1 when there are no matches, which is the passing case.
        offenders = [line for line in result.stdout.splitlines() if line and line != SELF_PATH]
        self.assertEqual(offenders, [], f"tracked files still reference {RETIRED_DOC}: {offenders}")


if __name__ == "__main__":
    unittest.main()
