"""Behavior checks for the Knowledge Base seed scripts and the KB smoke test.

Each script runs against a fake ``aws`` command; nothing here uses
credentials or cloud resources.

- seed-kb-<domain>.sh skips only when the stack does not exist; any other
  lookup error (for example expired credentials) fails instead of reporting an
  unseeded Knowledge Base as done. A COMPLETE ingestion job with failed
  documents fails.
- test-kb.sh fails when a retrieved chunk comes from outside the domain's own
  document prefix.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPOSITORY_ROOT / "scripts" / "infrastructure"
DOMAINS = ("gamelift", "eks", "cost")

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(" ".join(args))
state_path.write_text(json.dumps(state))
def opt(flag, default=""):
    return args[args.index(flag) + 1] if flag in args else default
query = opt("--query")
service, op = args[0], args[1]
if (service, op) == ("cloudformation", "describe-stacks"):
    mode = state.get("stack", "ok")
    if mode == "missing":
        print("An error occurred (ValidationError) when calling the DescribeStacks operation: "
              "Stack with id %s does not exist" % opt("--stack-name"), file=sys.stderr); sys.exit(254)
    if mode == "expired":
        print("An error occurred (ExpiredToken) when calling the AssumeRole operation: "
              "The security token included in the request is expired", file=sys.stderr); sys.exit(254)
    if "KnowledgeBaseId" in query: print("KBEXAMPLE01")
    elif "DataSourceId" in query: print("KBEXAMPLE01|DSEXAMPLE01")
    elif "DocumentBucketName" in query: print("example-kb-bucket")
    else: print("{}")
elif (service, op) == ("s3", "sync"):
    pass
elif (service, op) == ("bedrock-agent", "start-ingestion-job"):
    print("JOBEXAMPLE01")
elif (service, op) == ("bedrock-agent", "get-ingestion-job"):
    if query == "ingestionJob.status": print(state.get("status", "COMPLETE"))
    elif query == "ingestionJob.statistics.numberOfDocumentsFailed": print(state.get("failed", 0))
    elif query == "ingestionJob.failureReasons": print("Document exceeded the size limit")
    else: print("| numberOfDocumentsFailed | %s |" % state.get("failed", 0))
elif (service, op) == ("bedrock-agent-runtime", "retrieve"):
    uris = state["uris"]
    print("%d\t0.61\t%s" % (len(uris), ",".join(uris)))
'''


class _FakeAwsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "aws"
        fake.write_text(FAKE_AWS, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.state = self.tmp / "state.json"
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp),
            "FAKE_STATE": str(self.state),
            "AWS_REGION": "us-east-1",
            "AWS_PROFILE": "example",
        }

    def write_state(self, **values) -> None:
        self.state.write_text(json.dumps({"calls": [], **values}), encoding="utf-8")

    def calls(self) -> list[str]:
        return json.loads(self.state.read_text(encoding="utf-8"))["calls"]

    def run_script(self, script: Path) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(script)], env=self.env, capture_output=True, text=True, timeout=60)


class SeedScriptBehavior(_FakeAwsCase):
    def setUp(self) -> None:
        super().setUp()
        # The seed scripts find docs/kb-sources/<domain> relative to themselves.
        (self.tmp / "scripts" / "infrastructure").mkdir(parents=True)
        for domain in DOMAINS:
            shutil.copy(INFRA / f"seed-kb-{domain}.sh", self.tmp / "scripts" / "infrastructure")
            docs = self.tmp / "docs" / "kb-sources" / domain
            docs.mkdir(parents=True)
            (docs / "example.md").write_text("# Example\n", encoding="utf-8")

    def seed(self, domain: str) -> subprocess.CompletedProcess:
        return self.run_script(self.tmp / "scripts" / "infrastructure" / f"seed-kb-{domain}.sh")

    def test_a_missing_stack_is_skipped(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                self.write_state(stack="missing")
                result = self.seed(domain)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("not found, skipping seeding", result.stdout)

    def test_any_other_stack_lookup_error_fails_before_uploading(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                self.write_state(stack="expired")
                result = self.seed(domain)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ExpiredToken", result.stdout)
                self.assertFalse(any(c.startswith("s3 sync") for c in self.calls()))

    def test_completed_ingestion_with_failed_documents_fails(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                self.write_state(failed=2)
                result = self.seed(domain)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("failed documents: 2", result.stdout)
                self.assertIn("Document exceeded the size limit", result.stdout)

    def test_completed_ingestion_without_failures_succeeds(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                self.write_state(failed=0)
                result = self.seed(domain)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sync = next(c for c in self.calls() if c.startswith("s3 sync"))
                self.assertIn(f"s3://example-kb-bucket/{domain}/", sync)
                self.assertIn("--delete", sync)

    def test_powershell_seed_matches_the_shell_checks(self) -> None:
        text = (REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Invoke-GameAgentKBSeed.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("-match 'does not exist'", text)
        self.assertIn("throw \"Could not read stack", text)
        self.assertIn("ingestionJob.statistics.numberOfDocumentsFailed", text)
        self.assertIn("if ($failedDocs -ne '0')", text)


class KbSmokeTestBehavior(_FakeAwsCase):
    def smoke(self) -> subprocess.CompletedProcess:
        return self.run_script(INFRA / "test-kb.sh")

    def test_passes_when_every_result_is_under_its_domain_prefix(self) -> None:
        # The fake returns the same URIs for every domain, so give each a URI
        # set per run.
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                self.write_state(uris=[f"s3://example-kb-bucket/{domain}/a.md", f"s3://example-kb-bucket/{domain}/b.md"])
                result = self.smoke()
                self.assertIn(f"all under {domain}/", result.stdout)

    def test_fails_when_a_result_comes_from_another_prefix(self) -> None:
        self.write_state(uris=["s3://example-kb-bucket/gamelift/a.md", "s3://example-kb-bucket/eks/b.md"])
        result = self.smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Results outside the gamelift/ prefix", result.stdout)
        self.assertIn("s3://example-kb-bucket/eks/b.md", result.stdout)

    def test_fails_when_a_result_is_outside_any_prefix(self) -> None:
        self.write_state(uris=["s3://example-kb-bucket/stray.md"])
        result = self.smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Results outside the gamelift/ prefix", result.stdout)


if __name__ == "__main__":
    unittest.main()
