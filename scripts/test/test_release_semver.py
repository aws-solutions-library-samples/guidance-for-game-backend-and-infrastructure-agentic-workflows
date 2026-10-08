"""Tag grammar and semver precedence contracts for the release workflow."""

# Standard library
import sys
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Local modules
from release import semver  # noqa: E402


class TagGrammarTests(unittest.TestCase):
    def test_accepts_stable_and_rc_tags(self):
        for value in ["v0.1.0", "v0.1.0-rc.1", "v1.2.3", "v10.20.30", "v0.0.0", "v0.1.0-rc.10"]:
            with self.subTest(value=value):
                self.assertTrue(semver.is_valid_tag(value))

    def test_rejects_grammar_violations(self):
        bad = [
            "0.1.0",  # missing v
            "v1.0",  # too few components
            "v01.0.0",  # leading zero major
            "v1.00.0",  # leading zero minor
            "v1.0.0+build",  # build metadata
            "v1.0.0-rc",  # rc without number
            "v1.0.0-rc.01",  # leading zero in rc
            "v1.0.0-beta.1",  # wrong prerelease id
            "v1.0.0-rc.1-rc.2",
            "V1.0.0",  # uppercase V
        ]
        for value in bad:
            with self.subTest(value=value):
                self.assertFalse(semver.is_valid_tag(value))

    def test_rejects_injection_and_whitespace(self):
        hostile = [
            "v1.0.0\n",
            " v1.0.0",
            "v1.0.0 ",
            "v1.0.0;rm -rf /",
            "v1.0.0$(whoami)",
            "v1.0.0`id`",
            "v1.0.0 && echo",
            "v1.0.0/../../etc",
            "refs/tags/v1.0.0",
            "v1.0.0\tx",
        ]
        for value in hostile:
            with self.subTest(value=value):
                self.assertFalse(semver.is_valid_tag(value))
                with self.assertRaises(semver.InvalidTagError):
                    semver.parse_tag(value)


class PrecedenceTests(unittest.TestCase):
    def test_prerelease_lower_than_release(self):
        self.assertTrue(semver.parse_tag("v0.1.0") > semver.parse_tag("v0.1.0-rc.9"))
        self.assertTrue(semver.parse_tag("v0.1.0-rc.1") < semver.parse_tag("v0.1.0-rc.2"))
        self.assertTrue(semver.parse_tag("v0.2.0") > semver.parse_tag("v0.1.9"))

    def test_strictly_greater_than_all(self):
        existing = ["v0.1.0-rc.1", "v0.1.0-rc.2"]
        self.assertTrue(semver.strictly_greater_than_all("v0.1.0", existing))
        self.assertTrue(semver.strictly_greater_than_all("v0.1.0-rc.3", existing))

    def test_equal_or_older_is_not_greater(self):
        existing = ["v0.1.0"]
        self.assertFalse(semver.strictly_greater_than_all("v0.1.0", existing))  # equal reuse
        self.assertFalse(semver.strictly_greater_than_all("v0.0.9", existing))  # older
        self.assertFalse(semver.strictly_greater_than_all("v0.1.0-rc.1", existing))  # prerelease of same

    def test_ignores_unparseable_existing_tags(self):
        existing = ["not-a-tag", "v0.0.1"]
        self.assertTrue(semver.strictly_greater_than_all("v0.1.0", existing))


if __name__ == "__main__":
    unittest.main()
