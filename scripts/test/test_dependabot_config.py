"""Dependabot configuration checks.

``backend/requirements.txt`` is generated from ``backend/uv.lock`` and CI
fails any change where the two disagree. The uv Dependabot updater still treats
the export as a manifest, so the uv entry must exclude it; otherwise Dependabot
opens export-only pull requests that can never pass the lockfile check.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
DEPENDABOT = PROJECT_ROOT / ".github" / "dependabot.yml"


def _update_entries(text: str) -> list[str]:
    """Split the ``updates:`` list into one text block per ``- package-ecosystem`` entry."""
    body = text.split("\nupdates:\n", 1)[1]
    entries = body.split('\n  - package-ecosystem: ')[1:]
    return ['  - package-ecosystem: ' + entry for entry in entries]


class DependabotConfigTests(unittest.TestCase):
    def test_backend_python_updates_use_the_uv_ecosystem_only(self) -> None:
        entries = _update_entries(DEPENDABOT.read_text(encoding="utf-8"))
        backend = [e for e in entries if 'directory: "/backend"' in e]
        self.assertEqual(1, len(backend), "expected exactly one /backend Dependabot entry")
        self.assertTrue(backend[0].startswith('  - package-ecosystem: "uv"'), backend[0][:80])

    def test_uv_entry_excludes_the_generated_requirements_export(self) -> None:
        entries = _update_entries(DEPENDABOT.read_text(encoding="utf-8"))
        uv = next(e for e in entries if e.startswith('  - package-ecosystem: "uv"'))
        exclude = uv.split("\n    exclude-paths:\n", 1)
        self.assertEqual(2, len(exclude), "the uv entry must declare exclude-paths")
        paths = []
        for line in exclude[1].splitlines():
            if not line.startswith("      - "):
                break
            paths.append(line[len("      - ") :].strip().strip('"'))
        self.assertIn("requirements.txt", paths)


if __name__ == "__main__":
    unittest.main()
