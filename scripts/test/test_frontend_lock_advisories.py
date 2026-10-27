"""Lockfile contracts for frontend dependencies with published security advisories.

``npm audit`` and Dependabot cannot raise a transitive dependency whose parent
caps it, so the fix is an ``overrides`` entry in ``ui/package.json``. These
tests keep the override and the resolved lockfile version from regressing.
Direct dependencies are fixed by raising their range in ``ui/package.json``;
those tests keep the declared floor and every resolved copy patched.
"""

import json
from pathlib import Path
import re
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
PACKAGE_JSON = PROJECT_ROOT / "ui" / "package.json"
PACKAGE_LOCK = PROJECT_ROOT / "ui" / "package-lock.json"

# package -> first patched version. GHSA-238p-pmpm-9mq7 fixes katex in 0.18.2, and
# GHSA-mrhx-c7xc-m3w5 fixes zod in 4.6.6; CopilotKit still declares zod 3.
MINIMUM_PATCHED = {"katex": (0, 18, 2), "zod": (4, 6, 6)}

# Direct dependency -> (first patched version, advisories). The declared range in
# ui/package.json must not admit an affected release.
DIRECT_MINIMUM_PATCHED = {
    "next": ((16, 4, 1), "GHSA-2464-fhr4-w9xw, GHSA-p3m5-59mp-jv5r"),
}


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-")[0].split(".")[:3])


def _installed_copies(lock: dict, name: str) -> dict[str, str]:
    return {
        path: entry["version"]
        for path, entry in lock["packages"].items()
        if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}")
    }


class FrontendLockAdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        self.lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))

    def test_every_installed_copy_is_at_or_above_the_patched_version(self) -> None:
        for name, minimum in MINIMUM_PATCHED.items():
            copies = _installed_copies(self.lock, name)
            self.assertTrue(copies, f"{name} is no longer in the lockfile; remove it from MINIMUM_PATCHED")
            for path, version in copies.items():
                self.assertGreaterEqual(
                    _version_tuple(version),
                    minimum,
                    f"{path} resolves to {version}, below the patched {'.'.join(map(str, minimum))}",
                )

    def test_overrides_pin_each_capped_transitive_dependency(self) -> None:
        overrides = self.package.get("overrides", {})
        for name, minimum in MINIMUM_PATCHED.items():
            self.assertIn(
                name,
                overrides,
                f"{name} is capped by a parent package; keep its override in ui/package.json",
            )
            self.assertIn(
                ".".join(map(str, minimum)),
                overrides[name],
                f"the {name} override must name the first patched version",
            )

    def test_direct_dependencies_resolve_to_a_patched_version(self) -> None:
        for name, (minimum, advisories) in DIRECT_MINIMUM_PATCHED.items():
            copies = _installed_copies(self.lock, name)
            self.assertTrue(copies, f"{name} is no longer in the lockfile; remove it from DIRECT_MINIMUM_PATCHED")
            for path, version in copies.items():
                self.assertGreaterEqual(
                    _version_tuple(version),
                    minimum,
                    f"{path} resolves to {version}, below the release that fixes {advisories}",
                )

    def test_direct_dependency_ranges_exclude_affected_releases(self) -> None:
        declared = {**self.package.get("dependencies", {}), **self.package.get("devDependencies", {})}
        for name, (minimum, advisories) in DIRECT_MINIMUM_PATCHED.items():
            self.assertIn(name, declared, f"{name} is no longer a direct dependency; update DIRECT_MINIMUM_PATCHED")
            spec = declared[name]
            match = re.fullmatch(r"[\^~]?(\d+\.\d+\.\d+)", spec)
            self.assertIsNotNone(match, f"{name} range {spec!r} must be an exact, caret, or tilde version")
            self.assertGreaterEqual(
                _version_tuple(match.group(1)),
                minimum,
                f"ui/package.json allows {name} {spec}, which admits releases affected by {advisories}",
            )


if __name__ == "__main__":
    unittest.main()
