"""Behavior tests for the memory-record cleanup runbook in the deployment guide.

The runbook in ``docs/DEPLOYMENT_GUIDE.md`` tells an operator how to remove the
AgentCore Memory records left under raw-subject actor IDs after the runtime
switches to a pseudonymous digest actor ID. These tests extract the runbook's
bash blocks and run them against a local fake ``aws`` CLI that emulates the real
CLI v2's paged, text-mode ``--query`` output. No credentials, AWS calls, or live
infrastructure are used.

The fake ``list-memory-records`` reproduces the service behavior observed on a
live memory resource whose records are written under a single-segment
``namespaces=[actorId]`` layout: ``--namespace`` matches a whole namespace path
segment (after an optional leading ``/`` is stripped), not an arbitrary
character prefix, so a one-character value such as ``a`` matches nothing. The
fake also serves ``list-actors`` and ``cognito-idp list-users`` so discovery can
build its candidate actor IDs from the live actor list and the user pool's
subjects.

They assert the properties the runbook must have:

1. Discovery finds leftover old namespaces by building candidate actor IDs from
   ``list-actors`` and the Cognito user pool's subjects, dropping 64-hex
   digests, and listing each candidate by exact namespace. It finds a leftover
   known only from ``list-actors`` and one known only from the user pool, and
   skips digests.
2. A one-character namespace value finds nothing (the real service does not
   match a character prefix of a single-segment namespace), so a reverted
   per-character discovery loop discovers no leftovers.
3. The delete covers only the exact old namespace, even when the whole-segment
   listing also returns a namespace nested under it (`<ns>/facts`) or a
   leading-slash form (`/<ns>`), in batches of at most 100.
4. The dry-run (list) steps delete nothing.
5. A failed source or a failed candidate listing aborts discovery with a
   non-zero status and no namespace on stdout, so a partial list is never
   mistaken for the complete set, and a paste keeps the operator's shell open
   rather than closing it.
6. The documented cleanup removes the working files and clears their variables,
   so a Step 1 re-run recreates them with mktemp at mode 0600.
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
# the old namespace as a full character prefix is present, so a prefix delete
# (rather than an exact-namespace delete) would also remove the digest's records
# and fail the exact-namespace test.
OLD_NS = "abc"
PREFIX_DIGEST_NS = "abc" + "0" * 61  # a 64-hex digest that starts with OLD_NS

# Leftover namespaces, each discoverable from exactly one source so a discovery
# that drops either source is caught: ACTOR_ONLY_NS still has an actor row (its
# events have not expired) but no live user; POOL_ONLY_NS has a user-pool subject
# but no actor row (its events expired). Both still hold long-term records.
ACTOR_ONLY_NS = "f1e2d3c4-b5a6-7890-1234-567890abcdef"
POOL_ONLY_NS = "7f3e9a10-1b2c-4d5e-8f90-a1b2c3d4e5f6"
# An upper-case-leading and an "anonymous" leftover, both present as actors.
UPPER_NS = "Z9-test"
ANON_NS = "anonymous"

# A plain 64-hex digest actor (kept) is never a leftover. It appears both as an
# actor and as a records namespace.
DIGEST_NS = "d4c3b2a1" + "0" * 56

# The fake aws CLI. It serves list-memory-records, list-actors, and
# cognito-idp list-users from fixtures, paging the output as the real CLI v2
# does and applying --query to each page in text mode. --namespace matches a
# whole namespace path segment (after stripping a leading "/"), not a character
# prefix. It records every batch-delete-memory-records request.
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


def paged_text(values):
    # The CLI follows nextToken itself and applies --query to each page in text
    # mode, so a single-column projection prints one tab-joined line per page.
    out_lines = []
    for start in range(0, len(values), PAGE_SIZE):
        page = [v for v in values[start : start + PAGE_SIZE] if v != ""]
        if page:
            out_lines.append("\t".join(page))
    if out_lines:
        print("\n".join(out_lines))


# list-actors: enumerate the actors the memory resource still tracks (those with
# unexpired events). A chosen failure flag makes it fail like expired creds.
if args[:2] == ["bedrock-agentcore", "list-actors"]:
    if state.get("fail_list_actors"):
        save()
        sys.stderr.write("ExpiredTokenException: the security token has expired\n")
        sys.exit(254)
    save()
    paged_text(list(state.get("actors", [])))
    sys.exit(0)

# cognito-idp list-users: the user pool's subjects. The runbook projects
# Users[].Attributes[?Name==`sub`].Value, which yields one subject per user.
if args[:2] == ["cognito-idp", "list-users"]:
    if state.get("fail_list_users"):
        save()
        sys.stderr.write("AccessDeniedException: not authorized to perform cognito-idp:ListUsers\n")
        sys.exit(254)
    save()
    paged_text(list(state.get("subjects", [])))
    sys.exit(0)

if args[:2] == ["bedrock-agentcore", "list-memory-records"]:
    namespace = option("--namespace") or ""
    query = option("--query") or ""
    fixtures = state["records"]  # {namespace: [recordId, ...]}

    # A chosen set of namespaces fails the listing, as expired credentials or a
    # missing permission would, so a test can exercise the discovery abort.
    if namespace in state.get("fail_namespaces", []):
        save()
        sys.stderr.write("ExpiredTokenException: the security token has expired\n")
        sys.exit(254)

    # --namespace matches a whole namespace path segment, not a character prefix.
    # Strip a single leading "/" from both the query value and each stored
    # namespace, then match when the stored namespace equals the value or falls
    # under it as a path hierarchy (value == first segment(s), split on "/").
    want = namespace[1:] if namespace.startswith("/") else namespace

    def segment_match(stored):
        norm = stored[1:] if stored.startswith("/") else stored
        if norm == want:
            return True
        return norm.startswith(want + "/")

    summaries = []
    for ns in sorted(fixtures):
        if segment_match(ns):
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

    # Walk the pages in order, emitting one tab-joined line per page for a single
    # column projection and one row per record for a multi-column one. A page
    # with no match prints nothing (no blank line).
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

    def _write_state(
        self,
        records: dict,
        actors: list | None = None,
        subjects: list | None = None,
        fail_namespaces: list | None = None,
        fail_list_actors: bool = False,
        fail_list_users: bool = False,
    ) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "records": records,
                    "actors": actors if actors is not None else list(records),
                    "subjects": subjects or [],
                    "calls": [],
                    "fail_namespaces": fail_namespaces or [],
                    "fail_list_actors": fail_list_actors,
                    "fail_list_users": fail_list_users,
                }
            )
        )

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_STATE"] = str(self.state_path)
        env["AWS_REGION"] = "us-west-2"
        env["MEMORY_ID"] = "gameagentruntime_mem-0a1b2c3d4e"
        env["AWS_PROFILE"] = "operator-test"
        env["USER_POOL_ID"] = "us-west-2_EXAMPLE01"
        env["OLD_ACTOR"] = OLD_NS
        # Keep the discovery working files inside the test temp dir.
        env["TMPDIR"] = str(self.tmp)
        return env

    def _run(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env=self._env(),
            cwd=self.tmp,
            capture_output=True,
            text=True,
        )

    def _run_paste(self, script: str) -> subprocess.CompletedProcess:
        # Run a block the way a terminal paste does: a plain "bash -c" with no
        # "-e", so a failed command does not end the shell. This distinguishes a
        # subshell "exit 1" (stops discovery, shell survives) from a caller-level
        # "exit 1" (ends the whole run) and sees a dropped "&&" (classifier runs
        # anyway), which "bash -euo pipefail -c" cannot.
        return subprocess.run(
            ["bash", "-c", script],
            env=self._env(),
            cwd=self.tmp,
            capture_output=True,
            text=True,
        )

    def test_discovery_finds_leftovers_from_actors_and_the_user_pool(self) -> None:
        # ACTOR_ONLY_NS has an actor row but no live user; POOL_ONLY_NS has a
        # user-pool subject but no actor row. Both still hold records, so
        # discovery must list both. A current digest (present as an actor and as
        # a subject-free records namespace) and a digest-prefixed digest must not
        # be discovered. A record nested under ACTOR_ONLY_NS ("<ns>/facts") is
        # returned by the whole-segment listing but must not inflate the
        # candidate's own count: the exact-namespace filter counts it as 1.
        self._write_state(
            records={
                DIGEST_NS: ["mem-" + "d" * 40],
                PREFIX_DIGEST_NS: ["mem-" + "p" * 40],
                ACTOR_ONLY_NS: ["mem-" + "u" * 40],
                ACTOR_ONLY_NS + "/facts": ["mem-" + "x" * 40],
                POOL_ONLY_NS: ["mem-" + "g" * 40],
                UPPER_NS: ["mem-" + "r" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            },
            # list-actors returns the digest, the actor-only leftover, the
            # upper-case and anonymous leftovers -- but NOT the pool-only one.
            actors=[DIGEST_NS, ACTOR_ONLY_NS, UPPER_NS, ANON_NS],
            # The user pool returns the digest's user and the pool-only leftover,
            # but not the actor-only one (that user was deleted from the pool).
            subjects=[DIGEST_NS, POOL_ONLY_NS],
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertEqual(result.returncode, 0, result.stderr)
        # Each leftover row is "<count>\t<namespace>".
        counts = {}
        discovered = set()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            count, ns = line.split("\t")
            discovered.add(ns)
            counts[ns] = int(count)
        self.assertIn(ACTOR_ONLY_NS, discovered, "a leftover known only from list-actors must be found")
        self.assertIn(POOL_ONLY_NS, discovered, "a leftover known only from the user pool must be found")
        self.assertIn(UPPER_NS, discovered)
        self.assertIn(ANON_NS, discovered)
        self.assertNotIn(DIGEST_NS, discovered, "a 64-hex digest actor must be skipped")
        self.assertNotIn(PREFIX_DIGEST_NS, discovered)
        self.assertEqual(
            counts[ACTOR_ONLY_NS],
            1,
            "the exact-namespace filter must count only the candidate's own record, not a nested one",
        )

    def test_discovery_does_not_abort_on_a_candidate_with_no_records(self) -> None:
        # Most candidates (current users whose only records are kept digests, or
        # pool subjects with nothing left behind) hold no old records. Listing a
        # zero-record candidate must be skipped, not treated as an error that
        # ends discovery.
        # One extra subject sorts last among the candidates (an all-f UUID) and
        # holds no records, so a discovery that classifies only the last-sorted
        # candidate -- or prints nothing when the last one has no records -- is
        # caught here rather than passing silently.
        self._write_state(
            records={ACTOR_ONLY_NS: ["mem-" + "u" * 40]},
            actors=[ACTOR_ONLY_NS],
            # Two extra subjects with no leftover records at all; the second
            # sorts after every real leftover.
            subjects=[POOL_ONLY_NS, "ffffffff-0000-4000-8000-000000000000"],
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertEqual(result.returncode, 0, result.stderr)
        discovered = {line.split("\t")[-1] for line in result.stdout.splitlines() if line.strip()}
        self.assertEqual(discovered, {ACTOR_ONLY_NS}, "only the candidate holding records is a leftover")

    def test_discovery_is_clean_when_every_actor_is_a_digest(self) -> None:
        # A fully upgraded deployment has only 64-hex digest actors and subjects.
        # Discovery must then succeed with no leftover and no incomplete message,
        # not fail because the digest filter removed every candidate.
        self._write_state(
            records={DIGEST_NS: ["mem-" + "d" * 40]},
            actors=[DIGEST_NS],
            subjects=[DIGEST_NS],
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "a fully upgraded store has no leftovers")
        self.assertNotIn("discovery is incomplete", result.stderr)

    def test_rerun_after_documented_cleanup_recreates_files_at_0600(self) -> None:
        # The Step 1 block creates its three working files with mktemp (mode
        # 0600) and reuses them while their variables stay set. The documented
        # cleanup both removes the files and clears the variables, so a later
        # Step 1 in the same shell makes fresh mktemp files at 0600 rather than
        # recreating the paths by redirection at the shell's umask. With the
        # variables left set, those redirections would land at 0644 under umask
        # 022, exposing every pool subject's raw ID to other local users on a
        # shared /tmp. Run discovery, the documented cleanup line, and discovery
        # again under umask 022, then require the recreated files to be 0600.
        self._write_state(
            records={ACTOR_ONLY_NS: ["mem-" + "u" * 40]},
            actors=[ACTOR_ONLY_NS],
            subjects=[POOL_ONLY_NS],
        )
        cleanup = self._documented_cleanup_line()
        report = (
            '\nfor f in "$RAW_CANDIDATES_FILE" "$CANDIDATES_FILE" "$LEFTOVERS_FILE"; do\n'
            '  printf "mode %s\\n" "$(stat -f "%Lp" "$f" 2>/dev/null || stat -c "%a" "$f")"\n'
            "done\n"
        )
        script = "umask 022\n" + self.blocks[1] + "\n" + cleanup + "\n" + self.blocks[1] + report
        result = self._run_paste(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        modes = [line.split()[1] for line in result.stdout.splitlines() if line.startswith("mode ")]
        self.assertEqual(len(modes), 3, f"expected three working files, saw: {result.stdout!r}")
        self.assertTrue(
            all(mode == "600" for mode in modes),
            f"a re-run after the documented cleanup must recreate 0600 files, saw {modes}",
        )

    def _documented_cleanup_line(self) -> str:
        # Extract the "rm -f ...; unset ..." command the Step 1 block documents
        # as a comment, so this test exercises exactly what the guide tells the
        # operator to run rather than a hand-copied duplicate.
        for line in self.blocks[1].splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped.startswith("rm -f") and "unset" in stripped:
                return stripped
        raise AssertionError("Step 1 does not document an 'rm -f ...; unset ...' cleanup line")

    def test_one_character_namespace_discovers_nothing(self) -> None:
        # The real service does not match a character prefix of a single-segment
        # namespace. A reverted per-character discovery loop would list every
        # one-character --namespace and find nothing, so pin that semantic in the
        # fake directly: a one-character --namespace returns no rows even when a
        # namespace starting with that character holds records.
        self._write_state(
            records={ACTOR_ONLY_NS: ["mem-" + "u" * 40], POOL_ONLY_NS: ["mem-" + "g" * 40]},
            actors=[],
            subjects=[],
        )
        probe = (
            'aws bedrock-agentcore list-memory-records --memory-id "$MEMORY_ID" '
            '--region "$AWS_REGION" --namespace "f" '
            "--query 'memoryRecordSummaries[].[namespaces[0],memoryRecordId]' --output text"
        )
        result = self._run(probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "",
            "a one-character --namespace must match no single-segment namespace",
        )

    def test_discovery_aborts_on_a_listing_failure(self) -> None:
        # When a candidate listing fails (expired credentials, a missing
        # permission), Step 1 must stop with a non-zero status and print nothing
        # on stdout, so a partial leftover list is never mistaken for complete.
        self._write_state(
            records={
                ACTOR_ONLY_NS: ["mem-" + "u" * 40],
                POOL_ONLY_NS: ["mem-" + "g" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            },
            actors=[ACTOR_ONLY_NS, ANON_NS],
            subjects=[POOL_ONLY_NS],
            fail_namespaces=[ANON_NS],
        )
        result = self._run(self.blocks[1])  # Step 1 discovery
        self.assertNotEqual(result.returncode, 0, "a failed listing must abort discovery")
        self.assertEqual(result.stdout.strip(), "", "no namespaces may be printed when discovery is incomplete")
        self.assertIn("discovery is incomplete", result.stderr)

    def test_discovery_aborts_when_list_actors_fails(self) -> None:
        self._write_state(
            records={ACTOR_ONLY_NS: ["mem-" + "u" * 40]},
            actors=[ACTOR_ONLY_NS],
            subjects=[POOL_ONLY_NS],
            fail_list_actors=True,
        )
        result = self._run(self.blocks[1])
        self.assertNotEqual(result.returncode, 0, "a failed list-actors must abort discovery")
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("discovery is incomplete", result.stderr)

    def test_discovery_aborts_when_list_users_fails(self) -> None:
        self._write_state(
            records={POOL_ONLY_NS: ["mem-" + "g" * 40]},
            actors=[],
            subjects=[POOL_ONLY_NS],
            fail_list_users=True,
        )
        result = self._run(self.blocks[1])
        self.assertNotEqual(result.returncode, 0, "a failed list-users must abort discovery")
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("discovery is incomplete", result.stderr)

    def test_source_failure_keeps_the_pasting_shell_open(self) -> None:
        # A paste runs Step 1 in the operator's own interactive shell, which has
        # no "-e", so the shell does not exit when a command fails. The subshell
        # around the two source calls, and the "&&" that gates the next stage on
        # their success, are what make a failed source stop discovery (non-zero
        # status, nothing on stdout) without ending the shell. Running the block
        # under "bash -c" (no -e) and then echoing a marker reproduces that: if
        # the source subshell were replaced by a "{ }" group, its "exit 1" would
        # end the whole run before the marker; if an "&&" were dropped, the next
        # stage would run after the failure.
        self._write_state(
            records={
                ACTOR_ONLY_NS: ["mem-" + "u" * 40],
                POOL_ONLY_NS: ["mem-" + "g" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            },
            actors=[ACTOR_ONLY_NS, ANON_NS],
            subjects=[POOL_ONLY_NS],
            fail_list_actors=True,
        )
        script = self.blocks[1] + '\necho "after status=$? file=${CANDIDATES_FILE:+set}"\n'
        result = self._run_paste(script)
        self.assertEqual(
            result.stdout.strip(),
            "after status=1 file=set",
            "a failed source must leave status 1, print no namespace, and keep the shell open",
        )
        self.assertIn("discovery is incomplete", result.stderr)

    def test_listing_failure_keeps_the_pasting_shell_open(self) -> None:
        # The per-candidate listing stage also runs inside a subshell, so a
        # listing that fails mid-discovery stops with a non-zero status and
        # prints no leftover row, while the operator's interactive shell stays
        # open. A "{ }" group here would turn that subshell "exit 1" into an exit
        # of the operator's own shell, ending the paste before the marker. Fail
        # one candidate's listing (not a source), paste under "bash -c" (no -e),
        # and require the marker to still print at status 1.
        self._write_state(
            records={
                ACTOR_ONLY_NS: ["mem-" + "u" * 40],
                POOL_ONLY_NS: ["mem-" + "g" * 40],
                ANON_NS: ["mem-" + "n" * 40],
            },
            actors=[ACTOR_ONLY_NS, ANON_NS],
            subjects=[POOL_ONLY_NS],
            fail_namespaces=[ACTOR_ONLY_NS],
        )
        script = self.blocks[1] + '\necho "after status=$? file=${CANDIDATES_FILE:+set}"\n'
        result = self._run_paste(script)
        self.assertEqual(
            result.stdout.strip(),
            "after status=1 file=set",
            "a failed listing must leave status 1, print no leftover row, and keep the shell open",
        )
        self.assertIn("discovery is incomplete", result.stderr)
        self.assertEqual(result.stdout.count("\t"), 0, "no leftover row may print once a listing has failed")

    def test_dry_run_steps_delete_nothing(self) -> None:
        self._write_state(
            records={PREFIX_DIGEST_NS: ["mem-" + "d" * 40], OLD_NS: ["mem-" + "o" * 40]},
            actors=[PREFIX_DIGEST_NS, OLD_NS],
            subjects=[],
        )
        self._run(self.blocks[1])  # Step 1
        self._run(self.blocks[2])  # Step 2 dry run
        state = self._state()
        self.assertNotIn("delete_batches", state, "dry-run steps must not delete")
        self.assertFalse(
            any(call[:2] == ["bedrock-agentcore", "batch-delete-memory-records"] for call in state["calls"]),
        )

    def test_delete_is_exact_namespace_only(self) -> None:
        # A current digest namespace has OLD_NS ("abc") as a full character
        # prefix, and the whole-segment listing for "abc" also returns records
        # nested under it ("abc/facts") and a leading-slash form ("/abc"). An
        # exact-namespace delete removes only OLD_NS's own record; dropping the
        # [?namespaces[0]=='abc'] filter would also delete the nested and
        # leading-slash records that the whole-segment listing returns.
        self._write_state(
            records={
                PREFIX_DIGEST_NS: ["mem-" + "d" * 40, "mem-" + "e" * 40],
                OLD_NS: ["mem-" + "o" * 40],
                OLD_NS + "/facts": ["mem-" + "f" * 40],
                "/" + OLD_NS: ["mem-" + "s" * 40],
            },
            actors=[PREFIX_DIGEST_NS, OLD_NS],
            subjects=[],
        )
        result = self._run(self.blocks[3])  # Step 3 delete
        self.assertEqual(result.returncode, 0, result.stderr)
        batches = self._state()["delete_batches"]
        deleted = [rid for batch in batches for rid in batch]
        self.assertEqual(deleted, ["mem-" + "o" * 40], "delete must cover only the exact namespace")

    def test_delete_batches_at_most_100(self) -> None:
        many = ["mem-" + f"{i:040d}" for i in range(250)]
        self._write_state(records={OLD_NS: many}, actors=[OLD_NS], subjects=[])
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
    fences: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.markdown = GUIDE_PATH.read_text(encoding="utf-8")
        start = cls.markdown.index(RUNBOOK_HEADING)
        rest = cls.markdown[start:]
        nxt = re.search(r"\n## ", rest)
        cls.section = rest[: nxt.start()] if nxt else rest
        blocks = re.findall(r"```bash\n(.*?)```", cls.section, flags=re.DOTALL)
        cls.bash = "\n".join(blocks)
        # Every command fence in the section, bash and PowerShell, for the option
        # allowlist. The two forms must stay aligned, so both are scanned.
        ps_blocks = re.findall(r"```powershell\n(.*?)```", cls.section, flags=re.DOTALL)
        cls.fences = "\n".join(blocks + ps_blocks)
        # Step 3 is the delete block: the one that calls batch-delete-memory-records.
        delete_blocks = [b for b in blocks if "batch-delete-memory-records" in b]
        assert delete_blocks, "no bash block calls batch-delete-memory-records"
        cls.delete_block = delete_blocks[0]

    def test_discovery_builds_candidates_from_actors_and_the_pool(self) -> None:
        # Discovery derives candidate actor IDs from the live actor list and the
        # Cognito user pool's subjects; both calls must appear.
        self.assertIn("aws bedrock-agentcore list-actors", self.bash, "discovery must list actors")
        self.assertIn("aws cognito-idp list-users", self.bash, "discovery must list user-pool subjects")

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

    def test_cli_options_are_limited_to_a_known_allowlist(self) -> None:
        # The CLI follows nextToken itself, so any option that caps or disables
        # pagination (--no-paginate, --max-items, the hidden --max-results, and
        # their unambiguous prefixes such as --no-pag or --max-i) would truncate
        # a listing and discover or delete only part of a namespace. Rather than
        # denylisting spellings the CLI silently accepts, assert that every long
        # option used in the section's command fences is one the runbook needs.
        allowed = {
            "--memory-id",
            "--region",
            "--namespace",
            "--query",
            "--output",
            "--records",
            "--actor-id",
            "--session-id",
            "--event-id",
            "--user-pool-id",
        }
        used = set(re.findall(r"(?<!\S)(--[a-zA-Z][a-zA-Z0-9-]*)", self.fences))
        unexpected = used - allowed
        self.assertEqual(
            unexpected,
            set(),
            f"unexpected CLI options in the runbook fences (truncation risk): {sorted(unexpected)}",
        )


if __name__ == "__main__":
    unittest.main()
