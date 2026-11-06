"""Lockfile contracts for frontend dependencies with published security advisories.

``npm audit`` and Dependabot cannot raise a transitive dependency whose parent
caps it, so the fix is an ``overrides`` entry in ``ui/package.json``. These
tests keep the override and the resolved lockfile version from regressing.
Direct dependencies are fixed by raising their range in ``ui/package.json``;
those tests keep the declared floor and every resolved copy patched. A
transitive dependency whose parent's range already admits the patched release
is raised in ``ui/package-lock.json`` alone, or leaves the lockfile with the
last parent that required it; those tests allow it to be absent but keep every
resolved copy patched. Nothing in ``ui/`` loads an OpenTelemetry package, and
the required vulnerability scan reads every production entry in the lockfile,
so ``ui/package.json`` declares none and the lockfile keeps out the
auto-instrumentation tree.
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

# Transitive dependency -> (first patched version, advisories). The parent's range
# admits the patched release, so the lockfile alone carries the fix. The package may
# be absent, but any copy that returns must be patched: systeminformation left the
# lockfile with the unused frontend OpenTelemetry packages, whose host-metrics
# instrumentation required ^5.31.6.
TRANSITIVE_MINIMUM_PATCHED = {
    "systeminformation": ((5, 33, 16), "GHSA-3f7j-7jwc-fxfj, GHSA-7648-jmwf-vw7j, GHSA-4vjg-vx72-qq4g"),
}

# Package scope -> what declaring it cost. Nothing in ui/ imports or preloads an
# OpenTelemetry package: the container starts `node server.js` with no preload, and
# Next.js uses its bundled copy of @opentelemetry/api when none is installed.
UNDECLARED_SCOPES = {
    "@opentelemetry/": (
        "the 14 removed in #676 had added 158 production lockfile entries, including systeminformation and "
        "@grpc/grpc-js"
    ),
}

# Package -> what it adds to the lockfile. No dependency, direct or transitive, may
# bring it back.
EXCLUDED_FROM_LOCK = {
    "@opentelemetry/auto-instrumentations-node": "dozens of instrumentation packages and systeminformation",
}

DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


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

    def test_transitive_dependencies_resolve_to_a_patched_version(self) -> None:
        for name, (minimum, advisories) in TRANSITIVE_MINIMUM_PATCHED.items():
            for path, version in _installed_copies(self.lock, name).items():
                self.assertGreaterEqual(
                    _version_tuple(version),
                    minimum,
                    f"{path} resolves to {version}, below the release that fixes {advisories}",
                )

    def test_unused_package_scopes_stay_undeclared(self) -> None:
        declared = {name for field in DEPENDENCY_FIELDS for name in self.package.get(field, {})}
        for scope, footprint in UNDECLARED_SCOPES.items():
            names = sorted(name for name in declared if name.startswith(scope))
            self.assertFalse(
                names,
                f"ui/package.json declares {', '.join(names)}, but nothing in ui/ loads {scope}* packages "
                f"({footprint}). To add them deliberately, load them in the same change and remove {scope!r} "
                "from UNDECLARED_SCOPES in scripts/test/test_frontend_lock_advisories.py",
            )

    def test_excluded_packages_stay_out_of_the_lockfile(self) -> None:
        for name, footprint in EXCLUDED_FROM_LOCK.items():
            paths = sorted(_installed_copies(self.lock, name))
            self.assertFalse(
                paths,
                f"ui/package-lock.json contains {', '.join(paths)}; {name} adds {footprint} to the entries the "
                f"required vulnerability scan reads. `npm ls {name}` shows what requires it; if a change needs it "
                "deliberately, remove it from EXCLUDED_FROM_LOCK in scripts/test/test_frontend_lock_advisories.py",
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
