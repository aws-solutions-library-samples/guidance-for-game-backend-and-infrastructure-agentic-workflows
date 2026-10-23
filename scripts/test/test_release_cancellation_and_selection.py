"""Behavioral regressions for cancellation delivery, cleanup, and check selection.

These tests pin the behaviors a cancelled or interrupted publication must have,
the required-check selection must have under scheduled skips and in-progress
reruns, and the operations-mode scan must have over every tracked file:

* a cancelled publish step must deliver its signal to Python so cleanup runs,
  which requires the workflow to ``exec`` the publisher (and a signal-level
  test over a real ``bash -e -c 'exec python3 ...'`` child proves delivery);
* cleanup must not itself be interruptible by the follow-up SIGTERM, must run
  inside the runner's ~10-second SIGKILL window (a total budget with a
  per-request timeout), must delete the tag ref before the draft, must print
  the possible leftovers before its first request, and an ambiguous or
  interrupted ``create_release`` must leave nothing a later run cannot find;
* required-check reads must follow every page and order runs by start time so
  neither 30 newer scheduled skips nor a newer in-progress run can hide the
  real result;
* the operations-mode proof must scan every tracked file regardless of suffix
  or encoding and fail closed on any unreadable path.

Standard library only; the assertions observe behavior, not import or setup.
"""

# Standard library
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Third-party packages
from _release_fakes import RecordingClient, completed_check  # noqa: E402

# Local modules
from release import artifacts as artifacts_mod  # noqa: E402
from release import github_client as github_client_mod  # noqa: E402
from release import manifest, publish, validate  # noqa: E402

COMMIT = "a" * 40
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"


def _publish_inputs(tag="v0.1.0"):
    arts, sums = artifacts_mod.assemble_artifacts(
        manifest_bytes=b'{"schema_version": "gbaw.release-manifest.v1"}\n',
        backend_sbom_bytes=b'{"spdx": "backend"}\n',
        frontend_sbom_bytes=b'{"spdx": "frontend"}\n',
    )
    return publish.PublishInputs(
        tag=tag,
        commit=COMMIT,
        release_name=tag,
        notes="# notes\n",
        artifacts=arts,
        sha256sums=sums,
        tagger_name="Release Bot",
        tagger_email="release@example.com",
        commit_date="2024-01-02T03:04:05Z",
    )


# ---------------------------------------------------------------------------
# The workflow must exec its Python steps so a cancel reaches Python.
# ---------------------------------------------------------------------------
class WorkflowExecsPythonStepsTests(unittest.TestCase):
    """Every ``python3 -m release.cli`` step must be exec'd.

    GitHub signals the step's ``bash -e`` wrapper on cancel; only ``exec``
    replaces that shell with Python so SIGINT/SIGTERM reach the publisher's
    handler and cleanup runs.
    """

    def setUp(self):
        self.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_every_release_cli_run_step_uses_exec(self):
        # Standard library
        import re

        # Each `run:` that invokes release.cli must do so via `exec python3`.
        bare = re.findall(r"run:\s*(?!exec\b)python3 -m release\.cli", self.text)
        self.assertEqual(bare, [], msg=f"release.cli steps not exec'd: {bare}")
        execed = re.findall(r"run:\s*exec python3 -m release\.cli", self.text)
        self.assertGreaterEqual(len(execed), 1, msg="no exec'd release.cli step found")

    def test_publish_step_execs_the_publisher(self):
        self.assertIn("run: exec python3 -m release.cli publish", self.text)


class CancelReachesPythonSignalTests(unittest.TestCase):
    """A real ``bash -e -c 'exec python3 ...'`` child, sent SIGINT, must let the
    publisher's cleanup run.

    This proves the exec contract at the process-signal boundary with stdlib
    only, without a 10-second cancel wait.
    """

    def _child_source(self, state_file: Path) -> str:
        scripts_root = str(SCRIPTS_ROOT)
        return textwrap.dedent(
            f"""
            import json, sys, time, signal
            sys.path.insert(0, {scripts_root!r})
            sys.path.insert(0, {scripts_root!r} + "/test")
            from _release_fakes import RecordingClient
            from release import artifacts as artifacts_mod
            from release import publish

            STATE = {str(state_file)!r}
            COMMIT = "a" * 40

            class SlowFinalClient(RecordingClient):
                def update_release(self, release_id, **kwargs):
                    # Block in the final PATCH so the signal lands mid-publish.
                    with open(STATE, "w") as handle:
                        json.dump({{"phase": "in:update_release"}}, handle)
                    time.sleep(30)
                    return super().update_release(release_id, **kwargs)

            arts, sums = artifacts_mod.assemble_artifacts(
                manifest_bytes=b'{{"schema_version": "gbaw.release-manifest.v1"}}\\n',
                backend_sbom_bytes=b'{{"spdx": "backend"}}\\n',
                frontend_sbom_bytes=b'{{"spdx": "frontend"}}\\n',
            )
            inputs = publish.PublishInputs(
                tag="v0.1.0", commit=COMMIT, release_name="v0.1.0", notes="# notes\\n",
                artifacts=arts, sha256sums=sums, tagger_name="Bot",
                tagger_email="release@example.com", commit_date="2024-01-02T03:04:05Z",
            )
            client = SlowFinalClient()
            client.main_head = COMMIT
            try:
                publish.publish_release(client, inputs)
                phase = "returned"
            except BaseException as error:
                phase = "raised:" + type(error).__name__
            with open(STATE, "w") as handle:
                json.dump({{
                    "phase": phase,
                    "tag_ref": "tags/v0.1.0" in client.existing_refs,
                    "releases": [{{"draft": r["draft"]}} for r in client.releases],
                }}, handle)
            """
        )

    def _run(self, *, use_exec: bool):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            child = Path(d) / "child.py"
            child.write_text(self._child_source(state), encoding="utf-8")
            prefix = "exec " if use_exec else ""
            cmd = f"{prefix}{sys.executable} -B {child}"
            proc = subprocess.Popen(["/bin/bash", "-e", "-c", cmd])
            # Wait for the child to be blocked inside the final PATCH.
            deadline = time.time() + 20
            while time.time() < deadline:
                if state.exists():
                    # Standard library
                    import json

                    try:
                        if json.loads(state.read_text()).get("phase") == "in:update_release":
                            break
                    except (ValueError, OSError):
                        pass
                time.sleep(0.05)
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            # Standard library
            import json

            try:
                return json.loads(state.read_text())
            except (ValueError, OSError):
                return {"phase": "unknown"}

    def test_exec_delivers_sigint_and_cleanup_runs(self):
        state = self._run(use_exec=True)
        # Python received the signal: cleanup ran, nothing stable left behind.
        self.assertTrue(str(state.get("phase", "")).startswith("raised"), msg=state)
        self.assertFalse(state.get("tag_ref", True), msg=f"tag ref left behind: {state}")
        self.assertEqual(state.get("releases"), [], msg=f"draft left behind: {state}")


# ---------------------------------------------------------------------------
# Cleanup must resist the follow-up SIGTERM and an ambiguous draft create.
# ---------------------------------------------------------------------------
class CleanupIgnoresSignalsTests(unittest.TestCase):
    def test_cleanup_blocks_signals_for_its_duration(self):
        # While _cleanup runs it must ignore SIGINT and SIGTERM so the runner's
        # follow-up SIGTERM cannot abort cleanup halfway.
        observed: dict[str, object] = {}

        class SlowCleanupClient(RecordingClient):
            def update_release(self, release_id, **kwargs):
                self.calls.append("update_release")
                raise RuntimeError("boom in final patch")

            def list_releases(self):
                # Only reached from _cleanup. The published-release check that
                # guards the tag-ref delete is the FIRST list_releases call
                # (before the draft-state lookup). Capture the active signal
                # handlers while cleanup is executing, then send ourselves a
                # SIGTERM.
                observed["sigint"] = signal.getsignal(signal.SIGINT)
                observed["sigterm"] = signal.getsignal(signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.1)  # give any (wrongly) active handler a chance to fire
                observed["survived_sigterm"] = True
                return super().list_releases()

        client = SlowCleanupClient()
        client.main_head = COMMIT
        # Publication fails in the final PATCH, then cleanup runs. If cleanup
        # does not block signals, the self-sent SIGTERM aborts it before
        # ``survived_sigterm`` is set.
        with self.assertRaises(BaseException):
            publish.publish_release(client, _publish_inputs())
        # Cleanup must have completed despite the SIGTERM mid-cleanup.
        self.assertTrue(observed.get("survived_sigterm"), msg="cleanup aborted by SIGTERM mid-cleanup")
        self.assertEqual(observed.get("sigint"), signal.SIG_IGN, msg=f"SIGINT not ignored: {observed.get('sigint')!r}")
        self.assertEqual(
            observed.get("sigterm"), signal.SIG_IGN, msg=f"SIGTERM not ignored: {observed.get('sigterm')!r}"
        )


class CleanupBudgetTests(unittest.TestCase):
    """Cleanup must finish inside the runner's ~10-second SIGKILL window.

    The budget runs each request under a per-request timeout clamped to the
    remaining total budget, deletes the tag ref before the draft, and prints
    the possible leftovers before the first request. A deterministic clock and
    timeout runner drive the proof without multi-second sleeps.
    """

    def _budget(self, *, total, per_request, step):
        # A clock that advances by ``step`` seconds each time it is read, so a
        # sequence of calls deterministically consumes the budget.
        ticks = {"now": 0.0}

        def clock():
            value = ticks["now"]
            ticks["now"] += step
            return value

        attempts: list[float] = []

        def run_with_timeout(func, seconds):
            # Record the per-request deadline each call was given, then run it.
            attempts.append(seconds)
            return func()

        budget = publish._CleanupBudget(
            total_budget=total, per_request=per_request, clock=clock, run_with_timeout=run_with_timeout
        )
        return budget, attempts

    def test_per_request_deadline_is_clamped_to_remaining_budget(self):
        # First request gets the full per-request timeout; once the clock has
        # consumed most of the budget, the deadline is clamped BELOW the
        # per-request timeout to what little is left.
        budget, attempts = self._budget(total=5.0, per_request=2.0, step=2.0)
        budget.call_with_timeout(lambda: "a")  # read start=0; remaining 5 -> min(2,5)=2
        budget.call_with_timeout(lambda: "b")  # remaining ~1.0 -> min(2, 1.0)=1.0 (clamped)
        self.assertEqual(attempts[0], 2.0)
        self.assertLess(attempts[1], 2.0, msg=f"second deadline not clamped below per-request: {attempts[1]}")
        self.assertGreater(attempts[1], 0.0)

    def test_exhausted_budget_raises_without_running(self):
        # Once the clock has passed the total budget, no further request runs.
        budget, attempts = self._budget(total=8.0, per_request=2.0, step=5.0)
        budget.call_with_timeout(lambda: "a")  # consumes to ~5s remaining ~3
        with self.assertRaises(TimeoutError):
            budget.call_with_timeout(lambda: "should not run")
        self.assertEqual(len(attempts), 1, msg="a request ran after the budget was exhausted")

    def test_default_budget_bounds_are_within_the_kill_window(self):
        self.assertLessEqual(publish._CLEANUP_TOTAL_BUDGET_SECONDS, 8.0)
        self.assertLessEqual(publish._CLEANUP_PER_REQUEST_TIMEOUT_SECONDS, 2.0)
        self.assertLess(publish._CLEANUP_TOTAL_BUDGET_SECONDS, 10.0)

    def test_interrupted_cleanup_uses_the_short_budget(self):
        # Through the real publish_release, a cancellation (KeyboardInterrupt)
        # must build the short 8 s / 2 s budget, observed from the REAL
        # _CleanupBudget the code constructs (its defaults included), so a
        # default change or a lost forwarding is caught.
        built = self._budget_built_through_publish(KeyboardInterrupt())
        self.assertEqual(built["total"], publish._CLEANUP_TOTAL_BUDGET_SECONDS)
        self.assertEqual(built["per_request"], publish._CLEANUP_PER_REQUEST_TIMEOUT_SECONDS)
        self.assertLessEqual(built["total"], 8.0)
        self.assertLessEqual(built["per_request"], 2.0)

    def test_sigterm_interrupt_also_uses_the_short_budget(self):
        # A _ReleaseInterrupted (raised by the SIGTERM handler) is also a
        # cancellation and must build the short budget. This pins that the
        # classification covers _ReleaseInterrupted and not only
        # KeyboardInterrupt.
        built = self._budget_built_through_publish(publish._ReleaseInterrupted("sigterm"))
        self.assertEqual(built["total"], publish._CLEANUP_TOTAL_BUDGET_SECONDS)
        self.assertEqual(built["per_request"], publish._CLEANUP_PER_REQUEST_TIMEOUT_SECONDS)

    def test_ordinary_failure_cleanup_uses_the_longer_budget(self):
        # An ordinary failure has no incoming SIGKILL, so through the real
        # publish_release it must build the longer 120 s / 30 s budget (not the
        # 8 s / 2 s cancel budget), observed from the REAL _CleanupBudget.
        built = self._budget_built_through_publish(RuntimeError("ordinary 500"))
        self.assertEqual(built["total"], publish._ORDINARY_CLEANUP_TOTAL_BUDGET_SECONDS)
        self.assertEqual(built["per_request"], publish._ORDINARY_CLEANUP_PER_REQUEST_TIMEOUT_SECONDS)
        self.assertGreater(built["total"], 10.0)
        self.assertGreaterEqual(built["per_request"], 30.0)

    def _budget_built_through_publish(self, error_to_raise):
        # Drive the REAL publish_release to failure in the final PATCH, then
        # observe the actual total/per-request the real _CleanupBudget is
        # constructed with (by wrapping its __init__), rather than reading the
        # kwargs a spy was handed. Because the real class fills its own
        # defaults, changing those defaults, or losing the trigger->budget
        # wiring, changes what this observes. The budget's request runner is
        # left untouched; the client is instantaneous, so no real sleeps occur.
        # Standard library
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        built: dict[str, float] = {}
        real_init = publish._CleanupBudget.__init__

        def recording_init(self, *args, **kwargs):
            # Force an immediate, non-sleeping request runner and a zero clock
            # so the end-to-end cleanup does no real waiting, then record the
            # total/per-request the code actually chose.
            kwargs["clock"] = lambda: 0.0
            kwargs["run_with_timeout"] = lambda func, _s: func()
            real_init(self, *args, **kwargs)
            built["total"] = self._total
            built["per_request"] = self._per_request

        class FinalPatchFailsClient(RecordingClient):
            def update_release(self, release_id, **kwargs):
                raise error_to_raise

        client = FinalPatchFailsClient()
        client.main_head = COMMIT
        with mock.patch.object(publish._CleanupBudget, "__init__", recording_init):
            buffer = io.StringIO()
            with redirect_stderr(buffer):
                with self.assertRaises(type(error_to_raise)):
                    publish.publish_release(client, _publish_inputs())
        self.assertIn("total", built, msg="cleanup never built a budget")
        return built

    def test_every_cleanup_request_goes_through_the_budget(self):
        # A recording budget must see EVERY client call cleanup makes (the GET
        # ref, the published-release check, the DELETE ref, the draft lookup,
        # the draft-state check, and the DELETE release). Routing any one of
        # them around the budget would let a stalled call escape the window.
        client = RecordingClient()
        client.main_head = COMMIT
        draft = client.create_release(
            tag_name="v0.1.0", target_commitish=COMMIT, name="v0.1.0", body="", draft=True, prerelease=False
        )
        tag_sha = "t" * 40
        client.create_ref(ref="refs/tags/v0.1.0", sha=tag_sha)
        client.calls.clear()

        routed = {"count": 0}

        def run_with_timeout(func, _seconds):
            routed["count"] += 1
            return func()

        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=run_with_timeout)
        result = publish.PublishResult(release_id=int(draft["id"]), tag_ref_created=True, published=False)
        # Count the client-facing calls cleanup makes by watching ``calls``.
        publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        client_calls = [c for c in client.calls if not c.startswith("get_branch_head_sha")]
        self.assertEqual(
            routed["count"],
            len(client_calls),
            msg=f"not every cleanup call went through the budget: routed={routed['count']} calls={client.calls}",
        )
        self.assertGreater(routed["count"], 0)

    def test_cleanup_deletes_ref_before_draft_and_prints_identifiers_first(self):
        # Drive _cleanup_body directly with a client that created both a draft
        # and a tag ref. Record the order of the delete calls and capture stderr.
        # Standard library
        import io
        from contextlib import redirect_stderr

        client = RecordingClient()
        client.main_head = COMMIT
        # Simulate a run that created a draft and a tag ref.
        draft = client.create_release(
            tag_name="v0.1.0", target_commitish=COMMIT, name="v0.1.0", body="", draft=True, prerelease=False
        )
        tag_sha = "t" * 40
        client.create_ref(ref="refs/tags/v0.1.0", sha=tag_sha)
        client.calls.clear()

        result = publish.PublishResult(release_id=int(draft["id"]), tag_ref_created=True, published=False)
        order: list[str] = []
        orig_delete_ref = client.delete_ref
        orig_delete_release = client.delete_release

        def rec_delete_ref(ref):
            order.append("delete_ref")
            return orig_delete_ref(ref)

        def rec_delete_release(rid):
            order.append("delete_release")
            return orig_delete_release(rid)

        client.delete_ref = rec_delete_ref
        client.delete_release = rec_delete_release

        buffer = io.StringIO()
        # An immediate pass-through budget so the deterministic ordering is the
        # only thing under test here.
        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=lambda func, _s: func())
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        err = buffer.getvalue()
        self.assertEqual(order, ["delete_ref", "delete_release"], msg=f"ref must be deleted before draft: {order}")
        # The possible-leftovers line is printed before any request, so it must
        # name the tag, tag-object sha, and the release id.
        self.assertIn("possible leftovers", err)
        self.assertIn("v0.1.0", err)
        self.assertIn(tag_sha, err)
        self.assertIn(str(draft["id"]), err)
        # Both were removed, so no manual-recovery line is printed; the positive
        # completion line is printed instead.
        self.assertNotIn("manual recovery required", err)
        self.assertIn("nothing left to remove", err)

    def test_completion_line_absent_and_tag_ref_left_intact_is_reported(self):
        # When the tag ref exists but points at a DIFFERENT object than this
        # run's tag (so it is not ours to delete), cleanup must report "tag ref
        # ... (left intact)" on the manual-recovery line AND must NOT print the
        # positive completion line.
        # Standard library
        import io
        from contextlib import redirect_stderr

        class ForeignRefClient(RecordingClient):
            def get_ref(self, ref):
                # A ref that exists but points at an unrelated object.
                return {"object": {"sha": "f" * 40}}

        client = ForeignRefClient()
        client.main_head = COMMIT
        tag_sha = "t" * 40
        result = publish.PublishResult(release_id=0, tag_ref_created=True, published=False)
        buffer = io.StringIO()
        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=lambda func, _s: func())
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        err = buffer.getvalue()
        self.assertIn("tag ref v0.1.0 (left intact)", err)
        self.assertIn("manual recovery required", err)
        self.assertNotIn("nothing left to remove", err)

    def test_completion_line_absent_and_published_release_left_intact_is_reported(self):
        # When the create_release draft was published server-side (final PATCH
        # applied, then a 500 reached the client), the release is no longer a
        # draft, so cleanup must report "published release ... (left intact)"
        # and must NOT print the completion line.
        # Standard library
        import io
        from contextlib import redirect_stderr

        client = RecordingClient()
        client.main_head = COMMIT
        draft = client.create_release(
            tag_name="v0.1.0", target_commitish=COMMIT, name="v0.1.0", body="", draft=True, prerelease=False
        )
        # The release is already published server-side (no draft remains).
        client.update_release(release_id=int(draft["id"]), draft=False, prerelease=False, make_latest=True)
        client.calls.clear()
        result = publish.PublishResult(release_id=int(draft["id"]), tag_ref_created=False, published=False)
        buffer = io.StringIO()
        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=lambda func, _s: func())
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=None, budget=budget)
        err = buffer.getvalue()
        self.assertIn(f"published release id={draft['id']} (left intact)", err)
        self.assertIn("manual recovery required", err)
        self.assertNotIn("nothing left to remove", err)
        # The published release must NOT be deleted.
        self.assertTrue([r for r in client.releases if not r.get("draft")], msg="a published release was deleted")

    def test_completion_line_never_prints_alongside_a_manual_recovery_line(self):
        # The positive completion line and the manual-recovery line are
        # mutually exclusive. Any cleanup that prints the manual-recovery line
        # must NOT also print "nothing left to remove". Exercise a run that
        # leaves a leftover (an unknown tag-ref state) and assert exactly one of
        # the two outcome lines appears.
        # Standard library
        import io
        from contextlib import redirect_stderr

        class RefReadFailsClient(RecordingClient):
            def get_ref(self, ref):
                raise RuntimeError("github api get-ref failed with status 500")

        client = RefReadFailsClient()
        client.main_head = COMMIT
        tag_sha = "t" * 40
        result = publish.PublishResult(release_id=0, tag_ref_created=True, published=False)
        buffer = io.StringIO()
        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=lambda func, _s: func())
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        err = buffer.getvalue()
        self.assertIn("manual recovery required", err)
        self.assertNotIn("nothing left to remove", err)

    def test_announcement_is_printed_before_the_first_client_call(self):
        # The possible-leftovers announcement must reach stderr BEFORE the first
        # client request, so an operator sees what to check even if a SIGKILL
        # lands mid-cleanup. Observe the order with a budget whose runner records
        # whether the announcement was already on stderr at the first call.
        # Standard library
        import io
        from contextlib import redirect_stderr

        client = RecordingClient()
        client.main_head = COMMIT
        draft = client.create_release(
            tag_name="v0.1.0", target_commitish=COMMIT, name="v0.1.0", body="", draft=True, prerelease=False
        )
        tag_sha = "t" * 40
        client.create_ref(ref="refs/tags/v0.1.0", sha=tag_sha)
        client.calls.clear()

        buffer = io.StringIO()
        announced_before_first_call = {"value": None}

        def run_with_timeout(func, _seconds):
            if announced_before_first_call["value"] is None:
                announced_before_first_call["value"] = "possible leftovers" in buffer.getvalue()
            return func()

        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=run_with_timeout)
        result = publish.PublishResult(release_id=int(draft["id"]), tag_ref_created=True, published=False)
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        self.assertIs(
            announced_before_first_call["value"],
            True,
            msg="the announcement must be printed before the first client call",
        )

    def test_short_real_alarm_bounds_a_stalled_request_on_the_main_thread(self):
        # Exercise the REAL SIGALRM runner (not an injected one) on the main
        # thread, with a tiny deadline, so a function that would block is cut.
        # Kept well under 1 s and not flaky: the inner call sleeps longer than
        # the deadline, the alarm must fire first.
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM unavailable on this platform")

        def stalls():
            time.sleep(5)
            return "should not get here"

        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            publish._run_with_alarm_timeout(stalls, 0.05)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0, msg=f"real alarm did not cut the stalled call promptly: {elapsed:.3f}s")

    def test_tag_ref_unknown_state_leftover_is_reported(self):
        # If the GET-ref read raises, the tag ref's state is unknown and must be
        # reported as a leftover so an operator checks it by hand.
        # Standard library
        import io
        from contextlib import redirect_stderr

        class RefReadFailsClient(RecordingClient):
            def get_ref(self, ref):
                raise RuntimeError("github api get-ref failed with status 500")

        client = RefReadFailsClient()
        client.main_head = COMMIT
        tag_sha = "t" * 40
        result = publish.PublishResult(release_id=0, tag_ref_created=True, published=False)
        buffer = io.StringIO()
        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=lambda func, _s: func())
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        err = buffer.getvalue()
        self.assertIn("manual recovery required", err)
        self.assertIn(f"tag ref v0.1.0 (cleanup state unknown)", err)

    def test_release_unknown_state_leftover_is_reported(self):
        # If the draft-state read raises, the release's state is unknown and
        # must be reported as a leftover so an operator checks it by hand.
        # Standard library
        import io
        from contextlib import redirect_stderr

        class DraftStateFailsClient(RecordingClient):
            def list_releases(self):
                raise RuntimeError("github api list-releases failed with status 500")

        client = DraftStateFailsClient()
        client.main_head = COMMIT
        # No tag ref created; only a draft id is known.
        result = publish.PublishResult(release_id=4242, tag_ref_created=False, published=False)
        buffer = io.StringIO()
        budget = publish._CleanupBudget(clock=lambda: 0.0, run_with_timeout=lambda func, _s: func())
        with redirect_stderr(buffer):
            publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=None, budget=budget)
        err = buffer.getvalue()
        self.assertIn("manual recovery required", err)
        self.assertIn("release id=4242 (cleanup state unknown)", err)


class AmbiguousDraftCreationTests(unittest.TestCase):
    def test_ambiguous_create_release_is_cleaned_up_by_tag(self):
        # create_release applied server-side but the client saw a 502. The
        # draft (no release id returned to the caller) must still be found and
        # removed during cleanup via a tag_name lookup.
        class AmbiguousDraftClient(RecordingClient):
            def create_release(self, **kwargs):
                super().create_release(**kwargs)  # draft exists server-side
                raise RuntimeError("github api create-release failed with status 502")

        client = AmbiguousDraftClient()
        client.main_head = COMMIT
        with self.assertRaises(RuntimeError):
            publish.publish_release(client, _publish_inputs())
        # The orphaned draft must not survive, and no tag ref was ever created.
        self.assertEqual([r for r in client.releases if r.get("draft")], [], msg=client.releases)
        self.assertNotIn("tags/v0.1.0", client.existing_refs)

    def test_failed_draft_lookup_after_ambiguous_create_is_reported(self):
        # create_release applied server-side (502) AND the follow-up tag_name
        # lookup raises. The possible draft must be recorded as a leftover and
        # printed so an operator deletes it by hand.
        # Standard library
        import io
        from contextlib import redirect_stderr

        class AmbiguousThenBlindClient(RecordingClient):
            def create_release(self, **kwargs):
                raise RuntimeError("github api create-release failed with status 502")

            def list_releases(self):
                # The cleanup lookup by tag_name cannot see server state.
                raise RuntimeError("github api list-releases failed with status 500")

        client = AmbiguousThenBlindClient()
        client.main_head = COMMIT
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            with self.assertRaises(RuntimeError):
                publish.publish_release(client, _publish_inputs())
        err = buffer.getvalue()
        self.assertIn("manual recovery required", err)
        self.assertIn("lookup failed", err)
        self.assertIn("v0.1.0", err)


class OrdinaryFailureCleanupTests(unittest.TestCase):
    """An ordinary (non-cancel) failure must remove the tag ref and draft even
    when the API is slow-but-responsive, because there is no incoming SIGKILL.

    The short 8 s / 2 s cancel budget would give up after ~4 requests at 2.5 s
    latency and leave a public tag ref and a draft behind; the longer ordinary
    budget (120 s / 30 s) removes both. A deterministic clock models the 2.5 s
    per-request latency without any real multi-second sleeps.
    """

    def _latency_budget(self, *, total, per_request, latency):
        # The clock only advances when a request runs (by ``latency`` seconds),
        # so N sequential requests consume N*latency of the total budget.
        ticks = {"now": 0.0}

        def clock():
            return ticks["now"]

        def run_with_timeout(func, seconds):
            # A request that would exceed its clamped deadline raises, modelling
            # a per-request timeout; otherwise it "takes" ``latency`` seconds.
            if latency > seconds:
                ticks["now"] += seconds
                raise TimeoutError("request exceeded its per-request timeout")
            ticks["now"] += latency
            return func()

        return publish._CleanupBudget(
            total_budget=total, per_request=per_request, clock=clock, run_with_timeout=run_with_timeout
        )

    def _run_cleanup(self, budget):
        client = RecordingClient()
        client.main_head = COMMIT
        draft = client.create_release(
            tag_name="v0.1.0", target_commitish=COMMIT, name="v0.1.0", body="", draft=True, prerelease=False
        )
        tag_sha = "t" * 40
        client.create_ref(ref="refs/tags/v0.1.0", sha=tag_sha)
        client.calls.clear()
        result = publish.PublishResult(release_id=int(draft["id"]), tag_ref_created=True, published=False)
        publish._cleanup_body(client, _publish_inputs(), result, created_tag_sha=tag_sha, budget=budget)
        return client

    def test_short_budget_abandons_leftovers_under_slow_api(self):
        # The cancel budget (8 s total, 2 s/request) at 2.5 s latency cuts every
        # request and leaves BOTH the ref and the draft behind.
        short = self._latency_budget(total=8.0, per_request=2.0, latency=2.5)
        client = self._run_cleanup(short)
        self.assertIn("tags/v0.1.0", client.existing_refs, msg="short budget should have left the ref behind")
        self.assertTrue(
            [r for r in client.releases if r.get("draft")], msg="short budget should have left the draft behind"
        )

    def test_long_ordinary_budget_removes_ref_and_draft_under_slow_api(self):
        # The ordinary budget (120 s total, 30 s/request) at 2.5 s latency
        # completes every request and removes both the ref and the draft.
        long = self._latency_budget(
            total=publish._ORDINARY_CLEANUP_TOTAL_BUDGET_SECONDS,
            per_request=publish._ORDINARY_CLEANUP_PER_REQUEST_TIMEOUT_SECONDS,
            latency=2.5,
        )
        client = self._run_cleanup(long)
        self.assertNotIn("tags/v0.1.0", client.existing_refs, msg="ordinary budget should remove the tag ref")
        self.assertEqual(
            [r for r in client.releases if r.get("draft")], [], msg="ordinary budget should remove the draft"
        )

    def test_publish_release_ordinary_failure_removes_ref_and_draft(self):
        # End-to-end: an ordinary RuntimeError in the final PATCH triggers
        # cleanup, which must select the LONG budget. A latency model gives each
        # cleanup request 2.5 s (no real sleeps): under the long 120 s / 30 s
        # budget every request completes and both the ref and draft are removed;
        # under the short 8 s / 2 s cancel budget each 2.5 s request would exceed
        # its 2 s per-request deadline and both would be left behind. So a
        # regression that gives the ordinary path the short budget fails here.
        # Standard library
        from unittest import mock

        class FinalPatchFailsClient(RecordingClient):
            def update_release(self, release_id, **kwargs):
                self.calls.append("update_release")
                raise RuntimeError("github api update-release failed with status 500")

        real_init = publish._CleanupBudget.__init__

        def latency_init(self, *args, **kwargs):
            ticks = {"now": 0.0}
            latency = 2.5

            def clock():
                return ticks["now"]

            def run_with_timeout(func, seconds):
                if latency > seconds:
                    ticks["now"] += seconds
                    raise TimeoutError("request exceeded its per-request timeout")
                ticks["now"] += latency
                return func()

            kwargs["clock"] = clock
            kwargs["run_with_timeout"] = run_with_timeout
            real_init(self, *args, **kwargs)

        client = FinalPatchFailsClient()
        client.main_head = COMMIT
        with mock.patch.object(publish._CleanupBudget, "__init__", latency_init):
            with self.assertRaises(RuntimeError):
                publish.publish_release(client, _publish_inputs())
        # The tag ref and draft created before the failing PATCH are removed:
        # only the long ordinary budget tolerates 2.5 s requests.
        self.assertNotIn("tags/v0.1.0", client.existing_refs)
        self.assertEqual([r for r in client.releases if r.get("draft")], [])

    def test_publish_release_classifies_the_cleanup_trigger(self):
        # Pin the trigger classification in publish_release: an ordinary
        # exception must call cleanup with interrupted=False (long budget); a
        # cancellation (KeyboardInterrupt / _ReleaseInterrupted) with
        # interrupted=True (short budget). All three triggers are exercised so a
        # classification that forgets _ReleaseInterrupted, or treats every
        # failure the same, fails here.
        # Standard library
        from unittest import mock

        captured: list[bool] = []
        real_cleanup = publish._cleanup

        def spy_cleanup(*args, **kwargs):
            captured.append(bool(kwargs.get("interrupted")))
            return real_cleanup(*args, **kwargs)

        class OrdinaryFailClient(RecordingClient):
            def update_release(self, release_id, **kwargs):
                raise RuntimeError("ordinary 500")

        class SigintFailClient(RecordingClient):
            def update_release(self, release_id, **kwargs):
                raise KeyboardInterrupt()

        class SigtermFailClient(RecordingClient):
            def update_release(self, release_id, **kwargs):
                raise publish._ReleaseInterrupted("sigterm")

        with mock.patch.object(publish, "_cleanup", spy_cleanup):
            client = OrdinaryFailClient()
            client.main_head = COMMIT
            with self.assertRaises(RuntimeError):
                publish.publish_release(client, _publish_inputs())
            self.assertEqual(captured[-1], False, msg="ordinary failure must not be classified as interrupted")

            client2 = SigintFailClient()
            client2.main_head = COMMIT
            with self.assertRaises(KeyboardInterrupt):
                publish.publish_release(client2, _publish_inputs())
            self.assertEqual(captured[-1], True, msg="a SIGINT cancellation must be classified as interrupted")

            client3 = SigtermFailClient()
            client3.main_head = COMMIT
            with self.assertRaises(publish._ReleaseInterrupted):
                publish.publish_release(client3, _publish_inputs())
            self.assertEqual(
                captured[-1], True, msg="a SIGTERM-triggered _ReleaseInterrupted must be classified as interrupted"
            )


# ---------------------------------------------------------------------------
# Check-run reads paginate and order by started_at; in-progress counts.
# ---------------------------------------------------------------------------
class CheckRunPaginationTests(unittest.TestCase):
    def test_list_check_runs_follows_every_page(self):
        # A context with 101 runs must be read across both pages (per_page=100).
        requested: list[str] = []

        def transport(request):
            requested.append(request.url)
            # Standard library
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(request.url).query)
            page = int(query.get("page", ["1"])[0])
            runs = (
                [completed_check("backend-tests", COMMIT) for _ in range(100)]
                if page == 1
                else [completed_check("backend-tests", COMMIT)]
            )
            body = ('{"total_count": 101, "check_runs": ' + _json_runs(runs) + "}").encode()
            return github_client_mod.Response(status=200, headers={}, body=body)

        client = github_client_mod.GitHubClient(owner="o", repo="r", token="t", transport=transport)
        runs = client.list_check_runs(COMMIT, check_name="backend-tests")
        self.assertEqual(len(runs), 101, msg=f"did not paginate; got {len(runs)}")
        self.assertTrue(any("per_page=100" in url for url in requested), msg=requested)
        self.assertTrue(any("page=2" in url for url in requested), msg=requested)


class CheckRunOrderingTests(unittest.TestCase):
    def _client_single_context(self):
        client = RecordingClient()
        client.main_head = COMMIT
        client.branch_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "backend-tests", "integration_id": 15368}]},
            }
        ]
        return client

    def test_thirty_newer_skips_do_not_hide_success(self):
        # 30 scheduled skips with LATER completed_at than the real success must
        # not win: selection must ignore skips and still find the success.
        client = self._client_single_context()
        skips = [
            completed_check("backend-tests", COMMIT, conclusion="skipped", completed_at=f"2026-10-07T15:{i:02d}:00Z")
            for i in range(30)
        ]
        success = completed_check("backend-tests", COMMIT, conclusion="success", completed_at="2026-10-07T03:00:00Z")
        client.check_runs = {"backend-tests": skips + [success]}
        conclusions = validate.validate_required_checks(client, COMMIT)
        self.assertEqual([c.name for c in conclusions], ["backend-tests"])

    def test_newer_in_progress_run_blocks_selection(self):
        # A newer in-progress run (no completed_at, status != completed) started
        # AFTER the earlier success must be the latest run, so validation fails
        # closed until it finishes.
        client = self._client_single_context()
        success = {
            "name": "backend-tests",
            "head_sha": COMMIT,
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 15368},
            "started_at": "2026-10-07T03:00:00Z",
            "completed_at": "2026-10-07T03:05:00Z",
        }
        in_progress = {
            "name": "backend-tests",
            "head_sha": COMMIT,
            "status": "in_progress",
            "conclusion": None,
            "app": {"id": 15368},
            "started_at": "2026-10-07T16:00:00Z",
            "completed_at": None,
        }
        client.check_runs = {"backend-tests": [success, in_progress]}
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)


# ---------------------------------------------------------------------------
# Operations-mode scan covers every tracked file, encoding, and unreadable path.
# ---------------------------------------------------------------------------
def _ops_fake_repo(root: Path):
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
        "The default is `disabled`.\n", encoding="utf-8"
    )


def _hermetic_git_env():
    # Isolate git from any global/system config so the scan's ``git ls-files``
    # behaves the same regardless of the developer's own git settings (a global
    # ``core.excludesFile`` or ``core.autocrlf``/``core.safecrlf`` would
    # otherwise change which paths ``git add`` accepts).
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git_init_add(root: Path, paths):
    env = _hermetic_git_env()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True, env=env)
    # ``-f`` forces staging even if a stray ignore rule would exclude a path.
    subprocess.run(["git", "-C", str(root), "add", "-f", *paths], check=True, capture_output=True, env=env)


class OperationsModeFullTreeScanTests(unittest.TestCase):
    """A reference anywhere outside Markdown and the exempt prefixes must fail
    closed when the scan covers every tracked file in any text encoding."""

    def _assert_fails(self, rel: str, text: str):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(manifest.OperationsModeProofError, msg=f"{rel} not caught"):
                manifest.prove_operations_mode_disabled(root)

    def test_powershell_module_fails_closed(self):
        self._assert_fails(
            "scripts/powershell/GameAgent.psm1",
            "$env:GBAW_OPERATIONS_MODE = if ($env:OPS) { $env:OPS } else { 'operate' }\n",
        )

    def test_root_wrapper_script_fails_closed(self):
        self._assert_fails("deploy-all.sh", 'export GBAW_OPERATIONS_MODE="${GBAW_OPERATIONS_MODE:-operate}"\n')

    def test_dockerfile_fails_closed(self):
        self._assert_fails("ui/Dockerfile", "ENV GBAW_OPERATIONS_MODE=operate\n")

    def test_typescript_route_fails_closed(self):
        self._assert_fails(
            "ui/src/pages/api/copilot/chat.ts", 'const m = process.env.GBAW_OPERATIONS_MODE ?? "operate";\n'
        )

    def test_backend_module_under_docs_dir_fails_closed(self):
        # A path containing docs/ anywhere must NOT be exempt unless it is a
        # top-level docs/ tree; a code module under backend/.../docs/ is scanned.
        self._assert_fails(
            "backend/src/operations/docs/defaults.py",
            'MODE = __import__("os").environ.get("GBAW_OPERATIONS_MODE", "operate")\n',
        )

    def test_utf16le_file_fails_closed(self):
        # A UTF-16LE file carrying the marker has interleaved NUL bytes that a
        # UTF-8 text read would mangle; the raw-byte scan must still catch it.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "scripts" / "powershell").mkdir(parents=True)
            payload = "$env:GBAW_OPERATIONS_MODE = 'operate'\n".encode("utf-16-le")
            (root / "scripts" / "powershell" / "Run.ps1").write_bytes(b"\xff\xfe" + payload)
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_utf16be_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            payload = "x=GBAW_OPERATIONS_MODE\n".encode("utf-16-be")
            (root / "backend" / "blob.dat").write_bytes(payload)
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_utf32le_file_fails_closed(self):
        # A UTF-32-LE file carrying the marker must be caught; dropping the
        # utf-32-le encoding from the scan would let it through.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            payload = "x=GBAW_OPERATIONS_MODE\n".encode("utf-32-le")
            (root / "backend" / "blob32le.dat").write_bytes(payload)
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_utf32be_file_fails_closed(self):
        # A UTF-32-BE file carrying the marker must be caught; dropping the
        # utf-32-be encoding from the scan would let it through.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            payload = "x=GBAW_OPERATIONS_MODE\n".encode("utf-32-be")
            (root / "backend" / "blob32be.dat").write_bytes(payload)
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_bomless_utf16be_file_starting_with_marker_fails_closed(self):
        # A BOM-less UTF-16-BE file whose FIRST bytes are the marker pins the
        # UTF-16-BE search specifically: dropping only utf-16-be would let it
        # pass, because a UTF-16-LE search does not match a BE marker that has
        # no neighbouring byte to realign on.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            payload = "GBAW_OPERATIONS_MODE".encode("utf-16-be")
            (root / "backend" / "be_first.dat").write_bytes(payload)
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_bomless_utf16le_file_ending_with_marker_fails_closed(self):
        # A BOM-less UTF-16-LE file whose LAST bytes are the marker pins the
        # UTF-16-LE search specifically: dropping only utf-16-le would let it
        # pass, because a UTF-16-BE search does not match an LE marker with no
        # trailing byte to realign on.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            payload = "GBAW_OPERATIONS_MODE".encode("utf-16-le")
            (root / "backend" / "le_last.dat").write_bytes(payload)
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_bomless_utf32le_file_that_is_exactly_the_marker_fails_closed(self):
        # A BOM-less UTF-32-LE file whose bytes are EXACTLY the marker (no
        # neighbouring character) pins the UTF-32-LE search specifically:
        # dropping only utf-32-le would let it pass, because a UTF-32-BE search
        # cannot realign on a bare LE marker with no adjacent byte.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            (root / "backend" / "le32_exact.dat").write_bytes("GBAW_OPERATIONS_MODE".encode("utf-32-le"))
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_bomless_utf32be_file_that_is_exactly_the_marker_fails_closed(self):
        # A BOM-less UTF-32-BE file whose bytes are EXACTLY the marker pins the
        # UTF-32-BE search specifically: dropping only utf-32-be would let it
        # pass, because a UTF-32-LE search cannot realign on a bare BE marker
        # with no adjacent byte.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            (root / "backend" / "be32_exact.dat").write_bytes("GBAW_OPERATIONS_MODE".encode("utf-32-be"))
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_fifo_fails_closed_without_hanging(self):
        # A FIFO (named pipe) with no writer would block forever if opened; the
        # scan must lstat it, never open it, and fail closed. Guard the whole
        # test with a hard timeout thread so a regression that opens the FIFO
        # is caught as a failure instead of hanging the suite.
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo unavailable on this platform")
        # Standard library
        import threading

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            fifo_path = root / "backend" / "pipe.fifo"
            try:
                os.mkfifo(fifo_path)
            except OSError:
                self.skipTest("cannot create a FIFO on this filesystem")

            outcome: dict[str, object] = {}

            def run():
                try:
                    manifest.prove_operations_mode_disabled(root)
                    outcome["result"] = "passed"
                except manifest.OperationsModeProofError:
                    outcome["result"] = "failed-closed"
                except BaseException as error:  # noqa: BLE001
                    outcome["result"] = f"error:{type(error).__name__}"

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive(), msg="scan hung on a FIFO (it was opened instead of lstat'd)")
            self.assertEqual(outcome.get("result"), "failed-closed", msg=f"FIFO not failed closed: {outcome}")

    def test_symlink_candidate_fails_closed_not_followed(self):
        # Any symlink candidate must fail the proof CLOSED without being read
        # or followed, in both listing modes. Its link text is not scanned
        # (that would miss a marker in the real target) and the link is not
        # followed (that could escape the tree). The release tree has no
        # symlinks, so a tracked symlink is an unexpected shape that stops the
        # proof. Covers a dangling link, a link whose text carries the marker,
        # and a link to a regular file -- all fail closed identically.
        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink unavailable on this platform")
        for target in ("does/not/exist", "GBAW_OPERATIONS_MODE/target", "docs/adr"):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                _ops_fake_repo(root)
                (root / "backend").mkdir(parents=True)
                try:
                    os.symlink(target, root / "backend" / "candidate.link")
                except (OSError, NotImplementedError):
                    self.skipTest("cannot create a symlink on this platform")
                with self.assertRaises(manifest.OperationsModeProofError, msg=f"symlink to {target!r} not caught"):
                    manifest.prove_operations_mode_disabled(root)

    def test_symlink_to_clean_regular_file_fails_closed_not_resolved(self):
        # A symlink whose (absolute) target is a CLEAN regular file must still
        # fail closed: the proof must not resolve the link and scan the target.
        # This specifically pins ``lstat`` (which sees the link itself) over
        # ``stat`` (which would follow to the clean target and let the symlink
        # candidate pass).
        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink unavailable on this platform")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            (root / "backend" / "realtarget.txt").write_text("totally clean content\n", encoding="utf-8")
            try:
                os.symlink(str(root / "backend" / "realtarget.txt"), root / "backend" / "candidate.link")
            except (OSError, NotImplementedError):
                self.skipTest("cannot create a symlink on this platform")
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_directory_symlink_fails_closed_in_git_mode(self):
        # A symlink to a directory must fail closed, exercised in GIT mode so
        # the scan actually reaches the candidate (``os.walk`` lists a
        # directory symlink under dirnames, so a walk-mode test would never
        # scan it). git ls-files reports the link as a tracked path, and the
        # lstat dispatch sees a symlink and fails closed without following it
        # into the target directory (which could escape the tree).
        if not hasattr(os, "symlink") or shutil.which("git") is None:
            self.skipTest("symlink or git unavailable")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            (root / "elsewhere").mkdir()
            try:
                os.symlink(str(root / "elsewhere"), root / "backend" / "dir.link")
            except (OSError, NotImplementedError):
                self.skipTest("cannot create a symlink on this platform")
            _git_init_add(root, ["docs", "backend/dir.link"])
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_gitlink_directory_candidate_fails_closed(self):
        # In git mode a submodule gitlink is listed as a directory PATH, and the
        # scan must fail closed on it rather than treating the directory as
        # empty: the submodule's own content is outside this checkout and could
        # carry the marker unscanned. Registered with ``git update-index
        # --cacheinfo`` so no commit, identity, or submodule fetch is needed.
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            env = _hermetic_git_env()
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True, env=env)
            subprocess.run(["git", "-C", str(root), "add", "-f", "docs"], check=True, capture_output=True, env=env)
            subprocess.run(
                ["git", "-C", str(root), "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},sub"],
                check=True,
                capture_output=True,
                env=env,
            )
            # Materialize the submodule directory with the marker inside it, so
            # a regression that returns b"" for the directory would miss it.
            (root / "sub").mkdir()
            (root / "sub" / "x.py").write_text('M = "GBAW_OPERATIONS_MODE"\n', encoding="utf-8")
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_git_mode_scans_tracked_files_only(self):
        # On a real git repository the scan lists TRACKED files via
        # ``git ls-files -z``: an untracked file carrying the marker passes
        # (it is not part of the committed tree) and a tracked one fails. Uses
        # ``git add -f`` only (no commit, no identity) and runs git with an
        # isolated global/system config, so the test behaves the same under any
        # developer's git settings.
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        env = _hermetic_git_env()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True, env=env)
            (root / "backend").mkdir(parents=True, exist_ok=True)
            # An UNTRACKED file carrying the marker must be ignored by git mode.
            (root / "backend" / "untracked.py").write_text('M = "GBAW_OPERATIONS_MODE"\n', encoding="utf-8")
            # Tracked, clean files: the proof passes.
            (root / "backend" / "clean.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", "docs", "backend/clean.py"],
                check=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")

            # A TRACKED file carrying the marker fails closed. Use a NON-``.py``
            # suffix so narrowing the listing to ``*.py`` (which would drop it)
            # is caught here, not masked by a ``.py`` name.
            tracked = root / "backend" / "tracked.conf"
            tracked.write_text('mode = "GBAW_OPERATIONS_MODE"\n', encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", "backend/tracked.conf"],
                check=True,
                capture_output=True,
                env=env,
            )
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)
            subprocess.run(
                ["git", "-C", str(root), "rm", "--cached", "-q", "backend/tracked.conf"],
                check=True,
                capture_output=True,
                env=env,
            )
            tracked.unlink()

            # A tracked path whose NAME contains a space and a newline must be
            # parsed from the NUL-delimited listing and scanned correctly. A
            # CLEAN file with that name must PASS: a regression that drops
            # ``-z`` and splits on newlines would read the C-quoted/split name,
            # lstat would fail, and the proof would raise -- so asserting the
            # clean file passes is what pins that ``-z`` is used and the exact
            # byte-for-byte name is read.
            clean_weird = "backend/a clean file\nwith newline.conf"
            (root / clean_weird).write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-f", clean_weird], check=True, capture_output=True, env=env)
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")

            # The same shape of name carrying the marker must still fail closed.
            marked_weird = "backend/a marked file\nwith newline.conf"
            (root / marked_weird).write_text('mode = "GBAW_OPERATIONS_MODE"\n', encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", marked_weird], check=True, capture_output=True, env=env
            )
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_unreadable_path_fails_closed(self):
        # A tracked file inside an unreadable directory must fail the proof
        # closed, not be silently skipped (walk fallback, no git tree).
        if os.geteuid() == 0:
            self.skipTest("cannot create an unreadable directory as root")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            locked = root / "backend" / "locked"
            locked.mkdir(parents=True)
            (locked / "inside.py").write_text("x = 1\n", encoding="utf-8")
            os.chmod(locked, 0o000)
            try:
                with self.assertRaises(manifest.OperationsModeProofError):
                    manifest.prove_operations_mode_disabled(root)
            finally:
                os.chmod(locked, 0o755)

    def test_unreadable_file_fails_closed(self):
        # A mode-000 tracked file must fail closed on read, not be skipped.
        if os.geteuid() == 0:
            self.skipTest("cannot create an unreadable file as root")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True)
            locked_file = root / "backend" / "secret.py"
            locked_file.write_text("x = 1\n", encoding="utf-8")
            os.chmod(locked_file, 0o000)
            try:
                with self.assertRaises(manifest.OperationsModeProofError):
                    manifest.prove_operations_mode_disabled(root)
            finally:
                os.chmod(locked_file, 0o644)

    def test_git_mode_file_in_unreadable_directory_fails_closed_at_lstat(self):
        # In GIT mode the listing comes from ``git ls-files`` (not a walk), so a
        # tracked file inside a mode-000 directory is still listed, and the
        # per-candidate ``lstat`` is what fails. This reaches the lstat-failure
        # branch of the scan (the walk-mode unreadable-directory test above
        # fails earlier, at ``os.walk``'s ``onerror``). Must fail closed, not
        # be silently skipped. Skipped as root (mode-000 does not block root).
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("cannot create an unreadable directory as root")
        env = _hermetic_git_env()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True, env=env)
            locked = root / "backend" / "locked"
            locked.mkdir(parents=True)
            (locked / "inside.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", "docs", "backend/locked/inside.py"],
                check=True,
                capture_output=True,
                env=env,
            )
            os.chmod(locked, 0o000)
            try:
                with self.assertRaises(manifest.OperationsModeProofError):
                    manifest.prove_operations_mode_disabled(root)
            finally:
                os.chmod(locked, 0o755)

    def test_top_level_docs_and_tooling_still_exempt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "docs" / "THREAT_MODEL.md").write_text("GBAW_OPERATIONS_MODE discussed\n", encoding="utf-8")
            (root / "scripts" / "release").mkdir(parents=True)
            (root / "scripts" / "release" / "manifest.py").write_text("GBAW_OPERATIONS_MODE\n", encoding="utf-8")
            (root / "scripts" / "test").mkdir(parents=True)
            (root / "scripts" / "test" / "test_x.py").write_text("GBAW_OPERATIONS_MODE=operate\n", encoding="utf-8")
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")

    def test_markdown_is_exempt_anywhere(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend").mkdir(parents=True, exist_ok=True)
            (root / "backend" / "NOTES.md").write_text("discuss GBAW_OPERATIONS_MODE here\n", encoding="utf-8")
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")

    def test_clean_binary_file_still_passes(self):
        # A binary file that does NOT carry the marker must not trip the scan.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "assets").mkdir(parents=True)
            (root / "assets" / "logo.png").write_bytes(bytes(range(256)) * 4)
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")


# ---------------------------------------------------------------------------
# Waiver references flow through ValidationResult into the manifest.
# ---------------------------------------------------------------------------
def _waiver_evidence_client():
    # Standard library
    import json as _json

    # Local modules
    from release import evidence as _ev

    client = RecordingClient()
    client.main_head = COMMIT
    gates = {name: "pass" for name in _ev.REQUIRED_GATES}
    gates["p0_p1_closed_or_waived"] = "waived"
    doc = {
        "schema_version": _ev.EVIDENCE_SCHEMA_VERSION,
        "commit": COMMIT,
        "gates": gates,
        "waivers": {"p0_p1_closed_or_waived": "#412"},
    }
    client.comments[55] = {
        "user": {"login": "alice"},
        "issue_url": f"https://api.github.com/repos/{client.owner}/{client.repo}/issues/537",
        "body": f"```json\n{_json.dumps(doc, indent=2)}\n```",
    }
    client.collaborator_permissions = {"alice": "admin"}
    return client


def _waiver_manifest_repo(root: Path):
    _ops_fake_repo(root)
    (root / "backend" / "src" / "agents").mkdir(parents=True)
    (root / "backend" / "src" / "agents" / "optimized_prompts.py").write_text("P='x'\n", encoding="utf-8")
    (root / "backend" / "uv.lock").write_text("l\n", encoding="utf-8")
    (root / "backend" / "requirements.txt").write_text("r\n", encoding="utf-8")
    (root / "ui").mkdir()
    (root / "ui" / "package-lock.json").write_text("{}\n", encoding="utf-8")


class WaiverPropagationTests(unittest.TestCase):
    def test_evidence_validation_surfaces_the_waiver_reference(self):
        # Validating a waived evidence comment must surface the waiver reference
        # (#412), not only the gate status, so it can reach the manifest.
        client = _waiver_evidence_client()
        returned = validate.validate_evidence_comment(
            client, comment_id=55, commit=COMMIT, scan_fn=lambda _b: [], expected_issue=537
        )
        flattened = repr(returned)
        self.assertIn("#412", flattened, msg=f"waiver reference not surfaced by validation: {returned!r}")

    def test_run_full_validation_carries_the_waiver_reference(self):
        # run_full_validation must forward waivers from validate_evidence_comment
        # into ValidationResult.waivers; dropping that forwarding leaves the
        # reference unavailable to the manifest build.
        client = _waiver_evidence_client()
        client.tags = ["v0.0.1"]
        client.branch_rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": name, "integration_id": 15368} for name in validate.REQUIRED_CHECK_NAMES
                    ]
                },
            }
        ]
        # Third-party packages
        from _release_fakes import completed_check as _completed

        for name in validate.REQUIRED_CHECK_NAMES:
            client.check_runs[name] = [_completed(name, COMMIT)]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            notes = root / "docs" / "releases" / "v0.1.0.md"
            notes.parent.mkdir(parents=True)
            notes.write_text(
                "# v0.1.0\nDefaults Capabilities Known issues Compatibility Deployment Rollback\n",
                encoding="utf-8",
            )
            result = validate.run_full_validation(
                client,
                root,
                commit=COMMIT,
                tag="v0.1.0",
                evidence_comment_id=55,
                evidence_issue=537,
                release_name="v0.1.0",
                comment_scan_fn=lambda _b: [],
                file_scan_fn=lambda _p: [],
            )
        self.assertEqual(result.waivers.get("p0_p1_closed_or_waived"), "#412")

    def test_manifest_binds_the_waiver_reference(self):
        # A published manifest for a waived gate must name the issue that
        # granted the waiver somewhere in its serialized bytes.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _waiver_manifest_repo(root)
            built = manifest.build_manifest(
                root,
                tag="v0.1.0",
                release_name="v0.1.0",
                commit=COMMIT,
                commit_date="2024-01-02T03:04:05Z",
                required_checks=[manifest.CheckRunResult(name="backend-tests", conclusion="success")],
                evidence_digest="d" * 64,
                gate_summary={"p0_p1_closed_or_waived": "waived"},
                waivers={"p0_p1_closed_or_waived": "#412"},
                attached_file_names=["SHA256SUMS", "release-manifest.json"],
            )
            serialized = manifest.serialize_manifest(built)
            self.assertIn(b"#412", serialized, msg="manifest does not bind the waiver issue reference")


def _json_runs(runs):
    # Standard library
    import json

    return json.dumps(runs)


if __name__ == "__main__":
    unittest.main()
