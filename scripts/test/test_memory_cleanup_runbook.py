"""Behavior tests for the memory-record cleanup runbook in the deployment guide.

The runbook in ``docs/DEPLOYMENT_GUIDE.md`` tells an operator how to remove the
AgentCore Memory records left under raw-subject actor IDs after the runtime
switches to a pseudonymous digest actor ID. These tests extract the runbook's
bash blocks and run them against a local fake ``aws`` CLI that emulates the real
CLI v2's paged, text-mode ``--query`` output. No credentials, AWS calls, or live
infrastructure are used.

They assert the properties the runbook must have:

1. Discovery finds inactive old namespaces from the records themselves
   (not from ``list-actors``, which drops actors once their events expire),
   including namespaces that start with a digit and with an upper-case letter.
2. The delete covers only the exact old namespace, even when a current digest
   namespace has the old namespace as a prefix, in batches of at most 100.
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

# The old namespace is a short raw actor ID. A current digest namespace that has
# the old namespace as a full prefix is present, so a prefix delete (rather than
# an exact-namespace delete) would also remove the digest's records and fail the
# exact-namespace test.
OLD_NS = "abc"
PREFIX_DIGEST_NS = "abc" + "0" * 61  # a 64-hex digest that starts with OLD_NS

# Discovery fixtures that do not share a single leading character: a raw UUID,
# a UUID that starts with a digit, an upper-case-leading actor, and "anonymous".
UUID_NS = "f1e2d3c4-b5a6-7890-1234-567890abcdef"
DIGIT_UUID_NS = "7f3e9a10-1b2c-4d5e-8f90-a1b2c3d4e5f6"
UPPER_NS = "Z9-test"
ANON_NS = "anonymous"

# A plain 64-hex digest actor (kept) is never a leftover.
DIGEST_NS = "d4c3b2a1" + "0" * 56

# The fake aws CLI. It serves list-memory-records from a per-namespace fixture as
# the real CLI v2 does: --namespace is a prefix filter, results are paged with
# nextToken, and --query is applied to each page in text mode (so a one-value
# projection prints one tab-joined line per page). It records every
# batch-delete-memory-records request and refuses list-actors so a test fails
# loudly if the runbook calls it.
AWS_FAKE = r"""
import json
import os
import re
import sys

PAGE_SIZE = 2  # emulate server-side paging; multiple pages per listing

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

    # A chosen set of prefixes fails the listing, as expired credentials or a
    # missing permission would, so a test can exercise the discovery abort.
    if namespace in state.get("fail_prefixes", []):
        save()
        sys.stderr.write("ExpiredTokenException: the security token has expired\n")
        sys.exit(254)

    # --namespace is a server-side prefix filter: build the flat, ordered list of
    # (namespace, recordId) summaries whose namespace starts with the prefix.
    summaries = []
    for ns in sorted(fixtures):
        if ns.startswith(namespace):
            for rid in fixtures[ns]:
                summaries.append((ns, rid))

    # A JMESPath [?namespaces[0]=='X'] filter restricts to an exact namespace.
    exact_match = re.search(r"namespaces\[0\]=='([^']*)'", query)
    exact = exact_match.group(1) if exact_match else None

    # Determine the projected columns after the optional [?...] filter.
    proj = query
    filt = re.search(r"\[\?.*?\]\.", query)
    if filt:
        proj = query[filt.end() - 1 :]
    if ".[" in proj:
        inner = proj[proj.index(".[") + 2 :]
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

    # The CLI follows nextToken itself and applies --query to each page in text
    # mode. Walk the pages in order, emitting one tab-joined line per page for a
    # single-column projection and one row per record for a multi-column one. A
    # page with no match prints nothing (no blank line).
    out_lines = []
    for start in range(0, max(len(summaries), 1), PAGE_SIZE):
        page = summaries[start : start + PAGE_SIZE]
        page_rows = []
        for ns, rid in page:
            if exact is not None and ns != exact:
                continue
            page_rows.append("\t".join(cell(c, ns, rid) for c in columns))
        if not page_rows:
            continue
        if len(columns) == 1:
            out_lines.append("\t".join(page_rows))
        else:
            out_lines.extend(page_rows)

    save()
    if out_lines:
        print("\n".join(out_lines))
    sys.exit(0)

if args[:2] == ["bedrock-agentcore", "batch-delete-memory-records"]:
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
    # Stop at the next level-2+ heading (the Teardown section follows).
    next_heading = re.search(r"\n#{2} ", rest)
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

    def _write_state(self, records: dict, fail_prefixes: list | None = None) -> None:
        self.state_path.write_text(json.dumps({"records": records, "calls": [], "fail_prefixes": fail_prefixes or []}))

    def _run(self, script: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_STATE"] = str(self.state_path)
        env["AWS_REGION"] = "us-west-2"
        env["MEMORY_ID"] = "gameagentruntime_mem-0a1b2c3d4e"
        env["AWS_PROFILE"] = "operator-test"
        env["OLD_ACTOR"] = OLD_NS
        # Keep the discovery working file inside the test temp dir.
        env["TMPDIR"] = str(self.tmp)
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env=env,
            cwd=self.tmp,
            capture_output=True,
            text=True,
        )

    def test_discovery_finds_inactive_old_namespaces(self) -> None:
        # Old namespaces with records but no live events (list-actors would miss
        # them): a UUID, a UUID that starts with a digit, an upper-case-leading
        # actor, and "anonymous". A current digest and a digest-prefixed digest
        # are present and must not be discovered.
        self._write_state(
            {
                DIGEST_NS: ["mem-" + "d" * 40],
                PREFIX_DIGEST_NS: ["mem-" + "p" * 40],
                UUID_NS: ["mem-" + "u" * 40],
                DIGIT_UUID_NS: ["mem-" + "g" * 40],
                UPPER_NS: ["mem-" + "r" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            }
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertEqual(result.returncode, 0, result.stderr)
        discovered = {line for line in result.stdout.splitlines() if line}
        self.assertIn(UUID_NS, discovered)
        self.assertIn(DIGIT_UUID_NS, discovered)
        self.assertIn(UPPER_NS, discovered)
        self.assertIn(ANON_NS, discovered)
        self.assertNotIn(DIGEST_NS, discovered)
        self.assertNotIn(PREFIX_DIGEST_NS, discovered)
        # Discovery never called list-actors.
        self.assertFalse(
            any(call[:2] == ["bedrock-agentcore", "list-actors"] for call in self._state()["calls"]),
            "discovery must not use list-actors",
        )

    def test_discovery_aborts_on_a_listing_failure(self) -> None:
        # When a listing fails (expired credentials, a missing permission), Step 1
        # must stop with a non-zero status and print nothing on stdout, so a
        # partial namespace list is never mistaken for the complete set. A
        # digit-leading namespace is listed before the failing "a" prefix, so a
        # mutation that only breaks the loop (leaving the collected rows to be
        # classified) is still caught by the empty-stdout assertion.
        self._write_state(
            {
                DIGIT_UUID_NS: ["mem-" + "g" * 40],
                UUID_NS: ["mem-" + "u" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            },
            fail_prefixes=["a"],
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertNotEqual(result.returncode, 0, "a failed listing must abort discovery")
        self.assertEqual(result.stdout.strip(), "", "no namespaces may be printed when discovery is incomplete")
        self.assertIn("discovery is incomplete", result.stderr)

    def test_dry_run_steps_delete_nothing(self) -> None:
        self._write_state({PREFIX_DIGEST_NS: ["mem-" + "d" * 40], OLD_NS: ["mem-" + "o" * 40]})
        self._run(self.blocks[1])  # Step 1
        self._run(self.blocks[2])  # Step 2 dry run
        state = self._state()
        self.assertNotIn("delete_batches", state, "dry-run steps must not delete")
        self.assertFalse(
            any(call[:2] == ["bedrock-agentcore", "batch-delete-memory-records"] for call in state["calls"]),
        )

    def test_delete_is_exact_namespace_only(self) -> None:
        # A current digest namespace has OLD_NS ("abc") as a full prefix. A prefix
        # delete would also remove the digest's records; an exact-namespace delete
        # removes only OLD_NS's record.
        self._write_state(
            {
                PREFIX_DIGEST_NS: ["mem-" + "d" * 40, "mem-" + "e" * 40],
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
    """Static guards on the runbook text, scoped to the Step 3 bash block so a
    regression in the delete cannot hide behind the same construct elsewhere."""

    markdown: str
    section: str
    delete_block: str
    bash: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.markdown = GUIDE_PATH.read_text(encoding="utf-8")
        start = cls.markdown.index(RUNBOOK_HEADING)
        rest = cls.markdown[start:]
        nxt = re.search(r"\n## ", rest)
        cls.section = rest[: nxt.start()] if nxt else rest
        blocks = re.findall(r"```bash\n(.*?)```", cls.section, flags=re.DOTALL)
        cls.bash = "\n".join(blocks)
        # Step 3 is the delete block: the one that calls batch-delete-memory-records.
        delete_blocks = [b for b in blocks if "batch-delete-memory-records" in b]
        assert delete_blocks, "no bash block calls batch-delete-memory-records"
        cls.delete_block = delete_blocks[0]

    def test_discovery_does_not_use_list_actors(self) -> None:
        # The prose explains why list-actors is avoided; no command may invoke it.
        self.assertNotIn("aws bedrock-agentcore list-actors", self.bash, "no bash block may call list-actors")

    def test_delete_uses_exact_namespace_filter(self) -> None:
        self.assertIn("namespaces[0]==", self.delete_block, "the delete must restrict to an exact namespace")

    def test_batches_capped_at_100(self) -> None:
        self.assertIn("+= 100", self.delete_block, "the delete must chunk the ID list by 100")

    def test_delete_builds_one_id_per_line(self) -> None:
        # Text-mode --query output is tab-joined per page; splitting on tabs is
        # what gives one record ID per array element.
        self.assertIn("tr '\\t' '\\n'", self.delete_block, "the delete must split tab-joined IDs into lines")

    def test_dry_run_shows_namespace_and_createdat(self) -> None:
        self.assertIn("createdAt", self.section, "the dry run must show createdAt")

    def test_no_pagination_truncation_flags(self) -> None:
        # The CLI follows nextToken itself; adding --no-paginate or --max-items
        # would truncate the listing to one page (or N items) and delete only
        # part of the namespace. Neither flag may appear in any bash block.
        self.assertNotIn("--no-paginate", self.bash, "no bash block may use --no-paginate (truncates to one page)")
        self.assertNotIn("--max-items", self.bash, "no bash block may use --max-items (truncates the listing)")


if __name__ == "__main__":
    unittest.main()
