"""Tests for the repository public-content scanner."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import check_public_content  # noqa: E402


class PublicContentScannerTests(unittest.TestCase):
    def rules_for(self, text: str, denylist: tuple[str, ...] = ()) -> set[str]:
        return {
            finding.rule
            for finding in check_public_content.scan_text("fixture.txt", text, denylist)
        }

    def test_allows_documented_synthetic_values(self) -> None:
        text = "\n".join(
            [
                "account=123456789012",
                "email=user@example.com",
                "access_key=AKIAIOSFODNN7EXAMPLE",
            ]
        )

        self.assertEqual(self.rules_for(text), set())

    def test_rejects_non_synthetic_identifiers(self) -> None:
        account_id = "210987" + "654321"
        email = "person@" + "sample.invalid"

        self.assertEqual(
            self.rules_for(f"{account_id}\n{email}"),
            {"non-synthetic-account-id", "non-synthetic-email"},
        )

    def test_rejects_internal_domain(self) -> None:
        internal_url = "https://code." + "amazon.com/example"

        self.assertEqual(self.rules_for(internal_url), {"internal-domain"})

    def test_rejects_credentials_without_echoing_values(self) -> None:
        access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        private_key = "-----BEGIN " + "PRIVATE KEY-----"
        token = "ghp_" + ("a" * 36)
        fine_grained_token = "github_" + "pat_" + ("a" * 20)
        jwt = ".".join(["eyJ" + ("a" * 10), "b" * 10, "c" * 10])

        self.assertEqual(
            self.rules_for("\n".join([access_key, private_key, token, jwt])),
            {"access-key", "github-token", "jwt", "private-key"},
        )
        self.assertEqual(
            self.rules_for(fine_grained_token),
            {"github-token"},
        )

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.txt"
            fixture.write_text(access_key, encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = check_public_content.main([str(fixture)])

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("public-content/access-key", output)
        self.assertNotIn(access_key, output)

    def test_rejects_missing_explicit_path(self) -> None:
        missing = "missing-public-content-fixture.txt"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = check_public_content.main([missing])

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("scan path does not exist", stderr.getvalue())

    def test_private_denylist_is_case_insensitive(self) -> None:
        private_term = "private" + "-codename"

        self.assertEqual(
            self.rules_for("Contains PRIVATE-CODENAME", (private_term,)),
            {"private-denylist"},
        )


if __name__ == "__main__":
    unittest.main()
