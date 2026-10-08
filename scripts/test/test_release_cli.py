"""CLI contracts for the two-build determinism gate.

``release.cli build-artifacts`` normalizes and assembles the artifact set from
two independent syft runs and must fail unless every artifact's bytes are
byte-identical across the two builds. These tests drive the CLI with fake SBOM
inputs and a stubbed validation so no network or syft is required.
"""

# Standard library
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Local modules
from release import cli  # noqa: E402
from release import validate as validate_mod  # noqa: E402

COMMIT = "a" * 40
COMMIT_DATE = "2026-10-07T02:57:43Z"


def _raw_sbom(created="2026-10-07T19:15:19Z", namespace="https://anchore.com/syft/dir/x"):
    return json.dumps(
        {
            "spdxVersion": "SPDX-2.3",
            "creationInfo": {"created": created, "creators": ["Tool: syft-1.50.0"]},
            "documentNamespace": namespace,
            "name": "component",
            "packages": [{"SPDXID": "SPDXRef-Package-x", "name": "x"}],
        }
    ).encode()


def _fake_repo(root: Path):
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
        "The default is `disabled`.\n", encoding="utf-8"
    )
    (root / "backend" / "src" / "agents").mkdir(parents=True)
    (root / "backend" / "src" / "agents" / "optimized_prompts.py").write_text("P='x'\n", encoding="utf-8")
    (root / "backend" / "uv.lock").write_text("l\n", encoding="utf-8")
    (root / "backend" / "requirements.txt").write_text("r\n", encoding="utf-8")
    (root / "ui").mkdir()
    (root / "ui" / "package-lock.json").write_text("{}\n", encoding="utf-8")


class _StubResult(validate_mod.ValidationResult):
    pass


def _stub_validation():
    return validate_mod.ValidationResult(
        tag="v0.1.0",
        commit=COMMIT,
        release_name="v0.1.0",
        evidence_digest="d" * 64,
        gate_summary={"a": "pass"},
        check_conclusions=[validate_mod.CheckRunConclusion(name="backend-tests", conclusion="success")],
    )


class TwoBuildDeterminismTests(unittest.TestCase):
    def _run(self, root, env):
        with (
            mock.patch.object(cli, "REPO_ROOT", root),
            mock.patch.object(cli, "_run_validation", return_value=_stub_validation()),
            mock.patch.object(cli, "_client", return_value=(mock.MagicMock(), "o", "r")),
            mock.patch.dict("os.environ", env, clear=False),
        ):
            return cli.cmd_build_artifacts(mock.MagicMock())

    def test_matching_builds_succeed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            _fake_repo(root)
            work = Path(d) / "work"
            work.mkdir()
            for name in ("b1", "b2"):
                (work / f"backend-{name}.json").write_bytes(_raw_sbom())
                (work / f"frontend-{name}.json").write_bytes(_raw_sbom(namespace="https://anchore.com/syft/dir/ui"))
            out = Path(d) / "out"
            env = {
                "GBAW_RELEASE_OUTPUT_DIR": str(out),
                "GBAW_RELEASE_COMMIT_DATE": COMMIT_DATE,
                "GBAW_RELEASE_BACKEND_SBOM": str(work / "backend-b1.json"),
                "GBAW_RELEASE_FRONTEND_SBOM": str(work / "frontend-b1.json"),
                "GBAW_RELEASE_BACKEND_SBOM_2": str(work / "backend-b2.json"),
                "GBAW_RELEASE_FRONTEND_SBOM_2": str(work / "frontend-b2.json"),
            }
            rc = self._run(root, env)
            self.assertEqual(rc, 0)
            self.assertTrue((out / "release-manifest.json").is_file())
            self.assertTrue((out / "SHA256SUMS").is_file())

    def test_divergent_builds_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            _fake_repo(root)
            work = Path(d) / "work"
            work.mkdir()
            # Build 1 and build 2 differ in a package name -> normalized bytes differ.
            (work / "backend-b1.json").write_bytes(_raw_sbom())
            (work / "frontend-b1.json").write_bytes(_raw_sbom(namespace="https://anchore.com/syft/dir/ui"))
            (work / "backend-b2.json").write_bytes(
                json.dumps(
                    {
                        "spdxVersion": "SPDX-2.3",
                        "creationInfo": {"created": "2026-10-07T19:15:19Z", "creators": ["Tool: syft-1.50.0"]},
                        "documentNamespace": "https://anchore.com/syft/dir/x",
                        "name": "component",
                        "packages": [{"SPDXID": "SPDXRef-Package-y", "name": "y"}],
                    }
                ).encode()
            )
            (work / "frontend-b2.json").write_bytes(_raw_sbom(namespace="https://anchore.com/syft/dir/ui"))
            out = Path(d) / "out"
            env = {
                "GBAW_RELEASE_OUTPUT_DIR": str(out),
                "GBAW_RELEASE_COMMIT_DATE": COMMIT_DATE,
                "GBAW_RELEASE_BACKEND_SBOM": str(work / "backend-b1.json"),
                "GBAW_RELEASE_FRONTEND_SBOM": str(work / "frontend-b1.json"),
                "GBAW_RELEASE_BACKEND_SBOM_2": str(work / "backend-b2.json"),
                "GBAW_RELEASE_FRONTEND_SBOM_2": str(work / "frontend-b2.json"),
            }
            with self.assertRaises(SystemExit):
                self._run(root, env)


if __name__ == "__main__":
    unittest.main()
