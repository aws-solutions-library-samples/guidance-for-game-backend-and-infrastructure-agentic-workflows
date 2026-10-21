"""Lockfile contracts for frontend dependencies with published security advisories.

``npm audit`` and Dependabot cannot raise a transitive dependency whose parent
caps it, so the fix is an ``overrides`` entry in ``ui/package.json``. These
tests keep the override and the resolved lockfile version from regressing.
"""

import json
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
PACKAGE_JSON = PROJECT_ROOT / "ui" / "package.json"
PACKAGE_LOCK = PROJECT_ROOT / "ui" / "package-lock.json"

# package -> first patched version (GHSA-238p-pmpm-9mq7 fixes katex in 0.18.2).
MINIMUM_PATCHED = {"katex": (0, 18, 2)}


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-")[0].split(".")[:3])


class FrontendLockAdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        self.lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))

    def test_every_installed_copy_is_at_or_above_the_patched_version(self) -> None:
        for name, minimum in MINIMUM_PATCHED.items():
            copies = {
                path: entry["version"]
                for path, entry in self.lock["packages"].items()
                if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}")
            }
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


if __name__ == "__main__":
    unittest.main()
