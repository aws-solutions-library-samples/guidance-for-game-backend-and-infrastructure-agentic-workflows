"""Lockfile contracts for backend dependencies with published security advisories.

Dependabot cannot raise a transitive uv dependency in ``backend/uv.lock``, so
these fixes are made with ``uv lock --upgrade-package``. These tests keep each
locked version, and the runtime pin exported to ``backend/requirements.txt``,
at or above the first patched release, so a later re-lock cannot regress them.
"""

from pathlib import Path
import re
import tomllib
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
UV_LOCK = PROJECT_ROOT / "backend" / "uv.lock"
REQUIREMENTS = PROJECT_ROOT / "backend" / "requirements.txt"

# package -> (first patched version, advisories, exported to requirements.txt)
MINIMUM_PATCHED = {
    "aiohttp": ((3, 14, 4), "GHSA-2g87-pp6c-29x5, GHSA-5wr8-gcqf-gqf3", True),
    "multidict": ((6, 9, 1), "GHSA-54p9-h82j-f925", True),
    "certifi": ((2026, 10, 19), "GHSA-6q48-6c7w-9gw2", True),
    "uv": ((0, 13, 4), "GHSA-hq85-5cpj-mr62", False),
    "virtualenv": ((21, 14, 13), "GHSA-vxj4-q4g9-crj7, GHSA-c978-59m8-c77v, GHSA-cx6f-vjhg-g3cc", False),
    "pypdf": (
        (6, 21, 2),
        "GHSA-39mj-jf6j-2qj3, GHSA-7mh2-4gg2-979f, GHSA-9mx8-4j7g-633p, GHSA-qfv9-64jg-mh7p, "
        "GHSA-6c7f-9265-g8fw, GHSA-gmh7-3g9r-mjh5, GHSA-jr96-gvgh-j3r4",
        True,
    ),
    "black": ((26, 10, 1), "GHSA-cg8m-r9f2-5wm2", False),
    "filelock": ((4, 2, 1), "GHSA-v4x8-3wr5-3wq8", False),
    "fastmcp": ((3, 4, 10), "GHSA-7xgj-8392-5q4r", True),
    "fastmcp-slim": ((3, 4, 10), "GHSA-7xgj-8392-5q4r", True),
    "setuptools": ((84, 1, 0), "GHSA-xmj3-9gf7-h52w", True),
    "authlib": (
        (1, 9, 1),
        "GHSA-6v38-4h7w-rj9h, GHSA-7vr2-2fgp-9w7p, GHSA-8m4g-j4wp-wgcg, GHSA-x4rv-c3wx-4cr8, "
        "GHSA-c5mf-xmp2-59rc, GHSA-fch4-3c6x-j4gc, GHSA-q686-2qmg-hqc7, GHSA-jj7h-c3hg-m23x",
        True,
    ),
    "dnspython": ((2, 9, 0), "GHSA-45c3-73f7-pv4m", True),
}


# package -> (first patched version, advisories). The package may be absent from
# the lock, but any locked copy must be patched. strands-agents-tools caps Pillow
# below 13.0.0, the first release that fixes these, so the backend depends on
# neither strands-agents-tools nor strands-agents-builder.
ABSENT_OR_PATCHED = {
    "pillow": ((13, 0, 0), "GHSA-8g5g-8vmr-9782 and the other Pillow advisories fixed in 13.0.0"),
}


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


class BackendLockAdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
        self.locked: dict[str, list[str]] = {}
        for package in lock["package"]:
            self.locked.setdefault(package["name"].lower(), []).append(package["version"])
        self.pins = dict(
            match.groups()
            for match in re.finditer(r"^([A-Za-z0-9_.-]+)==(\S+)", REQUIREMENTS.read_text(encoding="utf-8"), re.M)
        )

    def test_every_locked_copy_is_at_or_above_the_patched_version(self) -> None:
        for name, (minimum, advisories, _) in MINIMUM_PATCHED.items():
            versions = self.locked.get(name)
            self.assertTrue(versions, f"{name} is no longer in uv.lock; remove it from MINIMUM_PATCHED")
            for version in versions:
                self.assertGreaterEqual(
                    _version_tuple(version),
                    minimum,
                    f"uv.lock resolves {name} to {version}, below the release that fixes {advisories}",
                )

    def test_capped_packages_are_absent_or_patched(self) -> None:
        for name, (minimum, advisories) in ABSENT_OR_PATCHED.items():
            for version in self.locked.get(name, []):
                self.assertGreaterEqual(
                    _version_tuple(version),
                    minimum,
                    f"uv.lock resolves {name} to {version}, below the release that fixes {advisories}",
                )
            if name in self.pins:
                self.assertGreaterEqual(
                    _version_tuple(self.pins[name]),
                    minimum,
                    f"requirements.txt pins {name}=={self.pins[name]}, below the release that fixes {advisories}",
                )

    def test_runtime_requirements_pin_the_patched_version(self) -> None:
        for name, (minimum, advisories, exported) in MINIMUM_PATCHED.items():
            if not exported:
                self.assertNotIn(name, self.pins, f"{name} is a development tool and must not be exported")
                continue
            self.assertIn(name, self.pins, f"requirements.txt no longer pins {name}; regenerate it with uv export")
            self.assertGreaterEqual(
                _version_tuple(self.pins[name]),
                minimum,
                f"requirements.txt pins {name}=={self.pins[name]}, below the release that fixes {advisories}",
            )


if __name__ == "__main__":
    unittest.main()
