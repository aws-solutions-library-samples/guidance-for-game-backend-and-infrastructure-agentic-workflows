"""Behavior tests for the memory-record cleanup runbook in the deployment guide.

The runbook in ``docs/DEPLOYMENT_GUIDE.md`` tells an operator how to remove the
AgentCore Memory records left under raw-subject actor IDs after the runtime
switches to a pseudonymous digest actor ID. These tests extract the runbook's
bash blocks and run them against a local fake ``aws`` CLI that returns canned
paginated output. No credentials, AWS calls, or live infrastructure are used.

They assert the three properties the runbook must have:

1. Discovery finds an inactive old namespace from the records themselves
   (not from ``list-actors``, which drops actors once their events expire).
2. The delete covers only the exact old namespace, in batches of at most 100.
3. The dry-run (list) steps delete nothing.
"""

from __future__ import annotations

# Standard library
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GUIDE_PATH = REPOSITORY_ROOT / "docs" / "DEPLOYMENT_GUIDE.md"
RUNBOOK_HEADING = "### Clean Up Memory Records Left Under Raw-Subject Actor IDs"

# A current digest namespace (64 lowercase hex) that shares a one-character
# prefix with the old namespace, to prove the delete matches exactly and not by
# prefix.
DIGEST_NS = "a" + "0" * 63
OLD_NS = "a1b2c3d4-5e6f-7a8b-9c0d-e1f2a3b4c5d6"
ANON_NS = "anonymous"

# The fake aws CLI. It serves list-memory-records from a per-namespace fixture,
# records every batch-delete-memory-records request to a log, and refuses to run
# list-actors so a test fails loudly if the runbook ever depends on it again.
AWS_FAKE = r"""
import json
import os
import sys

state_path = os.environ["FAKE_STATE"]
with open(state_path) as handle:
    state = json.load(handle)

args = sys.argv[1:]
state.setdefault("calls", []).append(args)


def save():
    with open(state_path, "w") as handle:
        json.dump(state, handle)


def option(name, default=None):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return default


# The runbook must not use list-actors; it misses actors whose events expired.
if args[:2] == ["bedrock-agentcore", "list-actors"]:
    save()
    sys.stderr.write("list-actors must not be used by the cleanup runbook\n")
    sys.exit(3)

if args[:2] == ["bedrock-agentcore", "list-memory-records"]:
    namespace = option("--namespace") or ""
    query = option("--query") or ""
    fixtures = state["records"]  # {namespace: [recordId, ...]}
    import re as _re

    # --namespace is a server-side prefix filter.
    matched = {ns: ids for ns, ids in fixtures.items() if ns.startswith(namespace)}
    # A JMESPath [?namespaces[0]=='X'] filter restricts to an exact namespace.
    exact_match = _re.search(r"namespaces\[0\]=='([^']*)'", query)
    exact = exact_match.group(1) if exact_match else None
    # The projection is the bracketed list after the optional [?...] filter.
    # Strip a leading filter expression if present, then read the columns.
    proj = query
    filt = _re.search(r"\[\?.*?\]\.", query)
    if filt:
        proj = query[filt.end() - 1 :]
    columns = []
    if ".[" in proj:
        inner = proj[proj.index(".[") + 2 :]
        # Match balanced to the projection's closing bracket.
        depth = 1
        buf = []
        for ch in inner:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            buf.append(ch)
        columns = [c.strip() for c in "".join(buf).split(",") if c.strip()]
    else:
        columns = ["memoryRecordId"]

    def cell(name, ns, rid):
        if name == "namespaces[0]":
            return ns
        if name == "memoryRecordId":
            return rid
        if name == "createdAt":
            return "2026-01-01T00:00:00Z"
        return ""

    rows = []
    for ns, ids in matched.items():
        if exact is not None and ns != exact:
            continue
        for rid in ids:
            rows.append("\t".join(cell(c, ns, rid) for c in columns))
    save()
    if rows:
        print("\n".join(rows))
    sys.exit(0)

if args[:2] == ["bedrock-agentcore", "batch-delete-memory-records"]:
    # Collect every memoryRecordId=... token across the --records values.
    record_ids = []
    collecting = False
    for token in args:
        if token == "--records":
            collecting = True
            continue
        if collecting:
            if token.startswith("--"):
                collecting = False
                continue
            if token.startswith("memoryRecordId="):
                record_ids.append(token.split("=", 1)[1])
    state.setdefault("delete_batches", []).append(record_ids)
    save()
    if len(record_ids) > 100:
        sys.stderr.write("batch exceeds the 100-record cap\n")
        sys.exit(4)
    print(json.dumps({"successfulRecords": [{"memoryRecordId": r} for r in record_ids], "failedRecords": []}))
    sys.exit(0)

save()
sys.stderr.write("unexpected aws invocation: %s\n" % " ".join(args))
sys.exit(5)
"""


def _extract_bash_blocks(markdown: str, heading: str) -> list[str]:
    """Return the fenced ```bash blocks under the given heading section."""
    start = markdown.index(heading)
    rest = markdown[start + len(heading) :]
    # Stop at the next level-3+ heading (the Teardown section follows).
    next_heading = re.search(r"\n#{2,3} ", rest)
    section = rest[: next_heading.start()] if next_heading else rest
    return re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL)


class MemoryCleanupRunbookTests(unittest.TestCase):
    markdown: str
    blocks: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.markdown = GUIDE_PATH.read_text(encoding="utf-8")
        cls.blocks = _extract_bash_blocks(cls.markdown, RUNBOOK_HEADING)
        # setup/export block, Step 1 discovery, Step 2 dry run, Step 3 delete,
        # Step 4 optional events.
        assert len(cls.blocks) >= 4, f"expected at least 4 bash blocks, got {len(cls.blocks)}"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gbaw-memcleanup-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        # fake aws on PATH
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "aws"
        fake.write_text("#!/usr/bin/env python3\n" + AWS_FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.state_path = self.tmp / "state.json"

    def _state(self) -> dict:
        state: dict = json.loads(self.state_path.read_text())
        return state

    def _write_state(self, records: dict) -> None:
        self.state_path.write_text(json.dumps({"records": records, "calls": []}))

    def _run(self, script: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_STATE"] = str(self.state_path)
        env["AWS_REGION"] = "us-west-2"
        env["MEMORY_ID"] = "gameagentruntime_mem-0a1b2c3d4e"
        env["AWS_PROFILE"] = "operator-test"
        env["OLD_ACTOR"] = OLD_NS
        # The Step-1 scratch file is written relative to cwd (the test temp dir).
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env=env,
            cwd=self.tmp,
            capture_output=True,
            text=True,
        )

    def test_discovery_finds_inactive_old_namespace(self) -> None:
        # An old namespace with records but no live events (list-actors would
        # miss it). A current digest namespace sharing the 'a' prefix is present.
        self._write_state(
            {
                DIGEST_NS: ["mem-" + "d" * 40],
                OLD_NS: ["mem-" + "o" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            }
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertEqual(result.returncode, 0, result.stderr)
        discovered = {line for line in result.stdout.splitlines() if line}
        self.assertIn(OLD_NS, discovered)
        self.assertIn(ANON_NS, discovered)
        self.assertNotIn(DIGEST_NS, discovered)
        # Discovery never called list-actors.
        self.assertFalse(
            any(call[:2] == ["bedrock-agentcore", "list-actors"] for call in self._state()["calls"]),
            "discovery must not use list-actors",
        )

    def test_dry_run_steps_delete_nothing(self) -> None:
        self._write_state({DIGEST_NS: ["mem-" + "d" * 40], OLD_NS: ["mem-" + "o" * 40]})
        self._run(self.blocks[1])  # Step 1
        self._run(self.blocks[2])  # Step 2 dry run
        state = self._state()
        self.assertNotIn("delete_batches", state, "dry-run steps must not delete")
        self.assertFalse(
            any(call[:2] == ["bedrock-agentcore", "batch-delete-memory-records"] for call in state["calls"]),
        )

    def test_delete_is_exact_namespace_only(self) -> None:
        # The digest namespace shares the 'a' prefix with the old UUID namespace.
        self._write_state(
            {
                DIGEST_NS: ["mem-" + "d" * 40, "mem-" + "e" * 40],
                OLD_NS: ["mem-" + "o" * 40],
            }
        )
        result = self._run(self.blocks[3])  # Step 3 delete
        self.assertEqual(result.returncode, 0, result.stderr)
        batches = self._state()["delete_batches"]
        deleted = [rid for batch in batches for rid in batch]
        self.assertEqual(deleted, ["mem-" + "o" * 40], "delete must cover only the exact namespace")

    def test_delete_batches_at_most_100(self) -> None:
        many = ["mem-" + f"{i:040d}" for i in range(250)]
        self._write_state({OLD_NS: many})
        result = self._run(self.blocks[3])  # Step 3 delete
        self.assertEqual(result.returncode, 0, result.stderr)
        batches = self._state()["delete_batches"]
        self.assertTrue(batches, "expected delete batches")
        for batch in batches:
            self.assertLessEqual(len(batch), 100, "each batch must be at most 100 records")
        self.assertEqual(sum(len(b) for b in batches), 250)
        self.assertEqual([rid for b in batches for rid in b], many)


class MemoryCleanupRunbookStaticChecks(unittest.TestCase):
    """Static guards on the runbook text so the fixes do not silently regress."""

    markdown: str
    section: str
    bash: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.markdown = GUIDE_PATH.read_text(encoding="utf-8")
        start = cls.markdown.index(RUNBOOK_HEADING)
        rest = cls.markdown[start:]
        nxt = re.search(r"\n## ", rest)
        cls.section = rest[: nxt.start()] if nxt else rest
        cls.bash = "\n".join(re.findall(r"```bash\n(.*?)```", cls.section, flags=re.DOTALL))

    def test_discovery_does_not_use_list_actors(self) -> None:
        # The prose explains why list-actors is avoided; the commands must not
        # invoke it for record discovery or verification.
        self.assertNotIn("list-actors", self.bash, "no bash block may call list-actors")

    def test_delete_uses_exact_namespace_filter(self) -> None:
        self.assertIn("namespaces[0]==", self.section, "delete must restrict to an exact namespace")

    def test_batches_capped_at_100(self) -> None:
        self.assertIn("+= 100", self.section, "delete must chunk the ID list by 100")

    def test_dry_run_shows_namespace_and_createdat(self) -> None:
        self.assertIn("createdAt", self.section, "the dry run must show createdAt")


if __name__ == "__main__":
    unittest.main()
