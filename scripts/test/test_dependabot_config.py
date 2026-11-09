"""Dependabot configuration checks.

``backend/requirements.txt`` is generated from ``backend/uv.lock`` and CI
fails any change where the two disagree. The uv Dependabot updater still treats
the export as a manifest, so the uv entry must exclude it; otherwise Dependabot
opens export-only pull requests that can never pass the lockfile check.

React refuses to render unless ``react`` and ``react-dom`` are the exact same
version, so the ``/ui`` npm entry must propose them, and their type packages,
as one grouped update instead of separate pull requests that each fail.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
DEPENDABOT = PROJECT_ROOT / ".github" / "dependabot.yml"
REACT_PACKAGES = {"react", "react-dom", "@types/react", "@types/react-dom"}


def _update_entries(text: str) -> list[str]:
    """Split the ``updates:`` list into one text block per ``- package-ecosystem`` entry."""
    body = text.split("\nupdates:\n", 1)[1]
    entries = body.split('\n  - package-ecosystem: ')[1:]
    return ['  - package-ecosystem: ' + entry for entry in entries]


def _group_patterns(entry: str) -> dict[str, list[str]]:
    """Map each group in an update entry's ``groups:`` block to its ``patterns`` list."""
    parts = entry.split("\n    groups:\n", 1)
    if len(parts) != 2:
        return {}
    groups: dict[str, list[str]] = {}
    current = None
    in_patterns = False
    for line in parts[1].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 4:
            break
        if indent == 6 and stripped.endswith(":"):
            current = stripped[:-1]
            groups[current] = []
            in_patterns = False
        elif indent == 8:
            in_patterns = stripped == "patterns:"
        elif indent == 10 and in_patterns and current is not None and stripped.startswith("- "):
            groups[current].append(stripped[2:].strip().strip('"'))
    return groups


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

    def test_ui_npm_entry_groups_react_with_react_dom_and_their_types(self) -> None:
        entries = _update_entries(DEPENDABOT.read_text(encoding="utf-8"))
        ui_npm = [e for e in entries if e.startswith('  - package-ecosystem: "npm"') and 'directory: "/ui"' in e]
        self.assertEqual(1, len(ui_npm), "expected exactly one /ui npm Dependabot entry")
        groups = _group_patterns(ui_npm[0])
        react_groups = [name for name, patterns in groups.items() if REACT_PACKAGES & set(patterns)]
        self.assertEqual(1, len(react_groups), f"React packages must share one group, got {groups}")
        self.assertEqual(REACT_PACKAGES, set(groups[react_groups[0]]))


if __name__ == "__main__":
    unittest.main()
