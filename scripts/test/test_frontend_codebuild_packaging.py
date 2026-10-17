"""Behavior tests for packaging the frontend source for CodeBuild.

The archive is uploaded to S3, so it must never contain local environment
files or dependencies. These tests use a temporary git repository and no AWS.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "infrastructure" / "build-frontend-codebuild.sh"


@unittest.skipUnless(shutil.which("zip") and shutil.which("unzip") and shutil.which("git"), "zip, unzip, git required")
class FrontendPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="frontend-package-tests-")
        self.root = Path(self.temporary_directory.name)
        self.ui = self.root / "repo" / "ui"
        self.ui.mkdir(parents=True)
        files = {
            "Dockerfile": "FROM scratch\n",
            "package.json": "{}\n",
            "src/pages/index.tsx": "export default function Page() {}\n",
            ".gitignore": ".env.local\n.env.*.local\nnode_modules\n.next\n",
            ".env.local": "AWS_PROFILE=private\n",
            ".env.production.local": "SECRET=1\n",
            "node_modules/pkg/index.js": "module.exports = 1\n",
            ".next/build-manifest.json": "{}\n",
        }
        for name, content in files.items():
            path = self.ui / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.output = self.root / "source.zip"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def package(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), "package", str(self.ui), str(self.output)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )

    def entries(self) -> set[str]:
        with zipfile.ZipFile(self.output) as archive:
            return set(archive.namelist())

    def init_git(self) -> None:
        repo = self.ui.parent
        for args in (
            ["init", "-q"],
            ["add", "ui/Dockerfile", "ui/package.json", "ui/.gitignore"],
            ["-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True)

    def test_git_checkout_packages_tracked_and_untracked_unignored_files_only(self) -> None:
        self.init_git()

        result = self.package()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.entries(),
            {"Dockerfile", "package.json", ".gitignore", "src/pages/index.tsx"},
        )

    def test_deleted_tracked_file_is_skipped(self) -> None:
        self.init_git()
        (self.ui / "package.json").unlink()

        result = self.package()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("package.json", self.entries())

    def test_without_git_local_env_files_and_dependencies_are_excluded(self) -> None:
        result = self.package()

        self.assertEqual(result.returncode, 0, result.stdout)
        entries = self.entries()
        self.assertIn("src/pages/index.tsx", entries)
        self.assertFalse(any(name.startswith(("node_modules/", ".next/")) for name in entries), entries)
        self.assertFalse(any(name.endswith(".local") for name in entries), entries)

    def test_tracked_env_file_is_refused(self) -> None:
        self.init_git()
        subprocess.run(["git", "-C", str(self.ui.parent), "add", "-f", "ui/.env.local"], check=True)

        result = self.package()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to upload", result.stdout)
        self.assertFalse(self.output.exists())

    def test_build_rejects_an_unsafe_image_tag(self) -> None:
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "build", "bad tag;rm"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid image tag", result.stdout)


if __name__ == "__main__":
    unittest.main()
