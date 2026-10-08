"""Audited publication ordering for a milestone release.

The publish job never rebuilds artifacts. It consumes the exact set the
read-only ``build-artifacts`` job produced and verified to be byte-identical
across two independent builds, downloads it as a workflow artifact, and
publishes those verified bytes unchanged.

Ordering (publish job only, and only when ``dry_run`` is false, the dispatch
ref is ``main``, and validation succeeded):

1. load the downloaded artifact set from disk and verify it against
   ``SHA256SUMS`` (tamper check) and the exclusion guard (public-safety);
2. verify the manifest's tag, commit, and attached-file list equal the trusted
   dispatch inputs -- the downloaded bytes are never trusted to assert their
   own identity;
3. re-run the pre-write validations against the live API: commit is still
   ``main`` HEAD, tag ref still absent and strictly greater, required checks
   green, evidence bound; and the ``release`` environment exists with a
   required-reviewers protection rule (fail closed);
4. install the SIGTERM-to-exception handler, then create a **draft** release
   with notes and upload the verified assets (draft creation is inside the
   guarded block, so an ambiguous or interrupted creation is cleaned up);
5. create the annotated tag *object* then the ``refs/tags/<tag>`` ref at the
   exact commit;
6. publish the draft (``prerelease`` for ``-rc.N``).

Failure handling:

* if any step after the handler is installed fails, cleanup runs with SIGINT
  and SIGTERM blocked so the runner's follow-up SIGTERM cannot abort it;
* cleanup chooses its budget by what interrupted it: a cancellation (SIGINT /
  SIGTERM) runs under an 8-second total budget with a 2-second per-request
  timeout (shorter than the runner's ~10-second SIGKILL window); an ordinary
  failure runs under a 120-second total / 30-second per-request budget (there
  is no incoming kill). Either way the possible leftovers are printed before
  the first request, and a positive completion line is printed when everything
  announced was confirmed removed;
* the tag ref is deleted first, and only if *this run* created it and the ref
  still points at this run's tag object and the release was not published;
* the draft is deleted only while it is still a draft (located by ``tag_name``
  when an ambiguous ``create_release`` returned no id).

Cleanup never deletes a published release: if the publish PATCH was applied
server-side and then answered with an error, that release is reported as left
intact for manual verification rather than removed. On the cancel path, a failed
run never leaves a stable-looking tag it had time to remove; whatever is not
confirmed removed within the budget (including against a dead API) is announced
for manual recovery.
"""

from __future__ import annotations

# Standard library
import contextlib
import json
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from . import checksums, semver
from .artifact_guard import ALLOWED_ARTIFACT_NAMES, CHECKSUMS_NAME, MANIFEST_NAME, Artifact, assert_artifacts_safe
from .github_client import GitHubClient
from .validate import MAIN_BRANCH, ValidationError, _require, validate_environment_protected


@dataclass(frozen=True)
class PublishInputs:
    tag: str
    commit: str
    release_name: str
    notes: str
    artifacts: list[Artifact]
    sha256sums: bytes
    tagger_name: str
    tagger_email: str
    commit_date: str


@dataclass(frozen=True)
class PublishMetadata:
    """The dispatch-derived facts the publish job knows independently of bytes.

    These are the trusted inputs (tag, commit, release name, notes, tagger
    identity, commit date) against which the downloaded artifact set is checked
    before anything is written. The artifacts themselves are never trusted to
    carry these values; they are verified to *equal* them.
    """

    tag: str
    commit: str
    release_name: str
    notes: str
    tagger_name: str
    tagger_email: str
    commit_date: str


@dataclass
class PublishResult:
    release_id: int
    tag_ref_created: bool
    published: bool


def _content_type_for(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    return "text/plain"


def preflight(client: GitHubClient, inputs: PublishInputs) -> None:
    """Re-verify artifacts and the live ref state immediately before any write."""

    # 1. Artifact integrity and public-safety guard (fails closed).
    assert_artifacts_safe(inputs.artifacts)
    # SHA256SUMS covers every *other* artifact.
    covered = {a.name: a.data for a in inputs.artifacts if a.name != "SHA256SUMS"}
    checksums.verify_against_sums(inputs.sha256sums, covered)

    # 2. Re-verify main HEAD and tag absence (race close).
    head = client.get_branch_head_sha(MAIN_BRANCH)
    _require(head == inputs.commit, "main HEAD moved since validation; refusing to publish")
    _require(
        client.get_ref(f"tags/{inputs.tag}") is None,
        "tag ref appeared since validation; refusing to publish",
    )


def assert_manifest_byte_identical(*, downloaded_manifest: bytes, rebuilt_manifest: bytes) -> None:
    """Require the downloaded manifest to equal the one rebuilt at publish time.

    The build-time manifest is frozen into the uploaded set, but the publish job
    waits behind an environment approval that can last days. Rebuilding the
    manifest from the publish-time checkout and re-validation result and
    requiring byte equality closes the window in which an evidence edit during
    approval, or manifest tampering in the build job, would otherwise be
    published unnoticed.
    """

    if downloaded_manifest != rebuilt_manifest:
        raise ValidationError(
            "release manifest changed between build and publish; refusing to publish a non-reproducible manifest"
        )


def publish_release(client: GitHubClient, inputs: PublishInputs) -> PublishResult:
    """Execute the ordered publication with strict, outcome-aware cleanup.

    Cleanup decides from the live remote state, not local flags, and runs on any
    ``BaseException`` (including a cancelled step's ``KeyboardInterrupt`` /
    SIGTERM) so an interrupted run never leaves a stable-looking tag or a
    published release. The SIGTERM-to-exception handler is installed BEFORE the
    draft is created so an interrupt during draft creation is also cleaned up.
    """

    preflight(client, inputs)

    parsed = semver.parse_tag(inputs.tag)
    prerelease = parsed.is_prerelease

    result = PublishResult(release_id=0, tag_ref_created=False, published=False)
    created_tag_sha: str | None = None

    # Treat SIGTERM (GitHub sends it after SIGINT during cancellation) as an
    # exception so cleanup runs instead of an abrupt kill bypassing it. The
    # handler is installed before ANY write, including draft creation, so an
    # ambiguous or interrupted ``create_release`` is cleaned up by tag_name.
    previous_handler = _install_sigterm_handler()
    try:
        # 3. Create DRAFT release and upload assets. Inside the guarded block so
        # an interrupt or ambiguous response during creation triggers cleanup.
        draft = client.create_release(
            tag_name=inputs.tag,
            target_commitish=inputs.commit,
            name=inputs.release_name,
            body=inputs.notes,
            draft=True,
            prerelease=prerelease,
        )
        result.release_id = int(draft["id"])
        upload_url = str(draft.get("upload_url", ""))

        for artifact in sorted(inputs.artifacts, key=lambda a: a.name):
            client.upload_release_asset(
                upload_url,
                name=artifact.name,
                data=artifact.data,
                content_type=_content_type_for(artifact.name),
            )

        # 4. Re-check main HEAD and tag absence IMMEDIATELY before the tag
        # write (closes any movement during asset uploads), then create the
        # annotated tag object and the ref at the exact commit.
        head = client.get_branch_head_sha(MAIN_BRANCH)
        _require(head == inputs.commit, "main HEAD moved before tag write; refusing to tag")
        _require(
            client.get_ref(f"tags/{inputs.tag}") is None,
            "tag ref appeared before tag write; refusing to tag",
        )

        # The tagger date is deliberately the commit date (not publication
        # time) so the annotated tag object is byte-reproducible: re-running the
        # publish for the same commit produces an identical tag object.
        created_tag_sha = client.create_tag_object(
            tag=inputs.tag,
            message=f"{inputs.release_name}\n\nImmutable milestone release.",
            commit=inputs.commit,
            tagger_name=inputs.tagger_name,
            tagger_email=inputs.tagger_email,
            date=inputs.commit_date,
        )
        client.create_ref(ref=f"refs/tags/{inputs.tag}", sha=created_tag_sha)
        result.tag_ref_created = True

        # 5. Publish the draft (make_latest decided explicitly: a prerelease is
        # never latest; a stable tag is strictly greater than all, so latest).
        client.update_release(
            release_id=result.release_id, draft=False, prerelease=prerelease, make_latest=not prerelease
        )
        result.published = True
        return result
    except BaseException as error:
        # Choose the cleanup budget by WHAT interrupted us. A cancellation
        # (KeyboardInterrupt from the runner's SIGINT, or _ReleaseInterrupted
        # from the SIGTERM handler) races the runner's ~10 s SIGKILL, so it uses
        # the short budget. An ordinary failure has no incoming kill and gets a
        # longer budget so a slow-but-responsive API still has its leftovers
        # removed rather than left behind.
        interrupted = isinstance(error, (KeyboardInterrupt, _ReleaseInterrupted))
        _cleanup(client, inputs, result, created_tag_sha=created_tag_sha, interrupted=interrupted)
        raise
    finally:
        _restore_sigterm_handler(previous_handler)


class _ReleaseInterrupted(BaseException):
    """Raised from a SIGTERM handler so publication cleanup can run."""


# The GitHub runner sends SIGKILL about 10 seconds after the SIGINT that starts
# a step CANCELLATION. When cleanup is triggered by that cancel path (a
# KeyboardInterrupt from SIGINT or a _ReleaseInterrupted from the SIGTERM
# handler) it must finish inside that window, so it runs under a short total
# wall-clock budget with a short per-request timeout; whatever is not confirmed
# removed within the budget is printed as a leftover for manual recovery. These
# are deliberately smaller than the 10-second kill window and the HTTP client's
# own 30-second socket timeout.
_CLEANUP_TOTAL_BUDGET_SECONDS = 8.0
_CLEANUP_PER_REQUEST_TIMEOUT_SECONDS = 2.0

# An ORDINARY failure (an API error or an exception that is not a cancellation)
# has no incoming SIGKILL, so cleanup is bounded only by the publish job's own
# timeout (no job sets timeout-minutes, so the default 360-minute job timeout
# applies). It uses the HTTP client's normal 30-second socket timeout per
# request and a 120-second total budget, so a slow-but-responsive API still gets
# every leftover removed instead of giving up inside the cancel window and
# leaving a public tag ref and a draft behind. Against a dead API the budget is
# still spent and whatever is not confirmed removed is reported for manual
# recovery.
_ORDINARY_CLEANUP_TOTAL_BUDGET_SECONDS = 120.0
_ORDINARY_CLEANUP_PER_REQUEST_TIMEOUT_SECONDS = 30.0


_UNSET = object()


def _safe_set_handler(signum, handler):
    """Set a signal handler, returning the previous one, or ``_UNSET`` on failure.

    Signal handlers can only be installed on the main thread; off the main
    thread (or where the signal is unsupported) this is a no-op that returns
    ``_UNSET`` so the caller skips restoring it.
    """

    try:
        return signal.signal(signum, handler)
    except (ValueError, OSError, RuntimeError):  # pragma: no cover - non-main thread
        return _UNSET


@contextlib.contextmanager
def _signals_blocked():
    """Ignore SIGINT and SIGTERM for the duration of the block.

    Cleanup must not be interruptible: once an interrupt has already triggered
    cleanup, the runner's follow-up SIGTERM (7.5 s after SIGINT) must not abort
    it halfway and leave a stable-looking tag or an orphaned draft. Restores the
    previous dispositions on exit. A no-op on a non-main thread, where signal
    handlers cannot be installed.
    """

    saved_sigint = _safe_set_handler(signal.SIGINT, signal.SIG_IGN)
    saved_sigterm = _safe_set_handler(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        if saved_sigint is not _UNSET:
            _safe_set_handler(signal.SIGINT, saved_sigint)
        if saved_sigterm is not _UNSET:
            _safe_set_handler(signal.SIGTERM, saved_sigterm)


class _CleanupBudget:
    """A wall-clock budget for cancellation cleanup.

    ``clock`` returns a monotonic number of seconds; ``call_with_timeout`` runs
    one callable under a per-request deadline that is clamped so it never
    exceeds the remaining total budget. A request that overruns its deadline, or
    a budget that is already exhausted, raises :class:`TimeoutError` so the
    caller records a leftover instead of waiting for the runner's SIGKILL. The
    clock and the per-request timeout runner are injectable so a test can prove
    the budget deterministically without multi-second sleeps.
    """

    def __init__(
        self,
        *,
        total_budget: float = _CLEANUP_TOTAL_BUDGET_SECONDS,
        per_request: float = _CLEANUP_PER_REQUEST_TIMEOUT_SECONDS,
        clock=None,
        run_with_timeout=None,
    ) -> None:
        self._total = float(total_budget)
        self._per_request = float(per_request)
        self._clock = clock if clock is not None else _monotonic
        self._run_with_timeout = run_with_timeout if run_with_timeout is not None else _run_with_alarm_timeout
        self._start = self._clock()

    def remaining(self) -> float:
        return self._total - (self._clock() - self._start)

    def call_with_timeout(self, func):
        """Run ``func`` under min(per_request, remaining budget); raise on overrun."""

        remaining = self.remaining()
        if remaining <= 0:
            raise TimeoutError("release cleanup budget exhausted")
        deadline = min(self._per_request, remaining)
        return self._run_with_timeout(func, deadline)


def _monotonic() -> float:
    # Standard library
    import time

    return time.monotonic()


def _run_with_alarm_timeout(func, seconds: float):
    """Run ``func`` under a best-effort SIGALRM deadline on the main thread.

    SIGALRM is not blocked by :func:`_signals_blocked` (which blocks only SIGINT
    and SIGTERM), so a stalled request still unwinds. Where SIGALRM is
    unavailable (non-main thread or an unsupported platform), the HTTP client's
    own socket timeout remains the only bound and ``func`` runs without the
    alarm. A fired alarm raises :class:`TimeoutError`.
    """

    if not hasattr(signal, "SIGALRM") or seconds <= 0:
        return func()

    def _on_alarm(_signum, _frame):  # pragma: no cover - timing dependent
        raise TimeoutError("release cleanup request exceeded its per-request timeout")

    # setitimer accepts sub-second deadlines; fall back to whole-second alarm().
    use_itimer = hasattr(signal, "setitimer")
    previous_alarm = None
    installed = False
    try:
        previous_alarm = signal.signal(signal.SIGALRM, _on_alarm)
        if use_itimer:
            signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.000001))
        else:
            signal.alarm(max(1, int(seconds)))
        installed = True
    except (ValueError, OSError, RuntimeError):  # pragma: no cover - non-main thread
        installed = False
    try:
        return func()
    finally:
        if installed:
            try:
                if use_itimer:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                else:
                    signal.alarm(0)
                if previous_alarm is not None:
                    signal.signal(signal.SIGALRM, previous_alarm)
            except (ValueError, OSError, RuntimeError):  # pragma: no cover
                pass


def _install_sigterm_handler():
    def _handler(_signum, _frame):
        raise _ReleaseInterrupted("received SIGTERM during publication")

    try:
        return signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError, RuntimeError):  # pragma: no cover - non-main thread
        return None


def _restore_sigterm_handler(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, RuntimeError):  # pragma: no cover
        pass


def _cleanup(
    client: GitHubClient,
    inputs: PublishInputs,
    result: PublishResult,
    *,
    created_tag_sha,
    interrupted: bool = True,
    budget=None,
) -> None:
    """Outcome-aware cleanup. Never raises; decides from live remote state.

    A client-visible error does not prove the server rejected the write: a 502
    after the server applied a PATCH, or an interrupt during the final publish,
    can leave a published release or a created ref. Cleanup therefore RE-READS
    the remote state and:

    * deletes the tag ref first (so a public tag never outlives its release),
      and only if it still points at THIS run's tag object;
    * deletes the release only if it is still a draft (never a published one).

    Cleanup IGNORES SIGINT and SIGTERM for its whole duration so the runner's
    follow-up SIGTERM (sent 7.5 s after SIGINT during cancellation) cannot abort
    it halfway. When ``interrupted`` is true (the cancel path) the GitHub runner
    sends SIGKILL about 10 s after SIGINT, so cleanup runs under the short
    8 s / 2 s budget and prints the possible leftovers (tag, tag-object SHA,
    release id) BEFORE its first request, so an operator sees what to check even
    if the kill lands mid-cleanup. An ordinary failure (``interrupted`` false)
    has no incoming kill and runs under the longer 120 s / 30 s budget so a slow
    API still has its leftovers removed. Whatever is not confirmed removed
    within the budget is printed as a leftover; when everything announced is
    confirmed removed, a positive completion line is printed instead. When no
    release id came back (an ambiguous ``create_release``), the draft is located
    by ``tag_name``.
    """

    with _signals_blocked():
        _cleanup_body(client, inputs, result, created_tag_sha=created_tag_sha, interrupted=interrupted, budget=budget)


def _cleanup_body(
    client: GitHubClient,
    inputs: PublishInputs,
    result: PublishResult,
    *,
    created_tag_sha,
    interrupted: bool = True,
    budget=None,
) -> None:
    if budget is None:
        if interrupted:
            budget = _CleanupBudget()
        else:
            budget = _CleanupBudget(
                total_budget=_ORDINARY_CLEANUP_TOTAL_BUDGET_SECONDS,
                per_request=_ORDINARY_CLEANUP_PER_REQUEST_TIMEOUT_SECONDS,
            )

    # Announce every identifier this run may have created BEFORE touching the
    # API, so even a SIGKILL mid-cleanup leaves an operator the exact things to
    # check. These are "possible" leftovers; the confirmed-removed ones are
    # subtracted from the final leftover report.
    possible: list[str] = []
    if created_tag_sha is not None:
        possible.append(f"tag ref {inputs.tag} -> tag object {created_tag_sha}")
    else:
        possible.append(f"tag ref {inputs.tag} (only if this run created it)")
    if result.release_id:
        possible.append(f"draft release id={result.release_id} for tag {inputs.tag}")
    else:
        possible.append(f"draft release for tag {inputs.tag} (id unknown)")
    print(
        "release cleanup starting; possible leftovers to verify: " + "; ".join(possible),
        file=sys.stderr,
    )

    leftovers: list[str] = []

    # Tag ref FIRST: a public tag with no release is the worst thing to leave.
    if created_tag_sha is not None:
        try:
            ref = budget.call_with_timeout(lambda: client.get_ref(f"tags/{inputs.tag}"))
            points_at_our_tag = ref is not None and str(ref.get("object", {}).get("sha", "")) == str(created_tag_sha)
            if points_at_our_tag and not budget.call_with_timeout(
                lambda: _published_release_exists(client, inputs.tag)
            ):
                budget.call_with_timeout(lambda: client.delete_ref(f"tags/{inputs.tag}"))
            elif ref is not None:
                leftovers.append(f"tag ref {inputs.tag} (left intact)")
        except Exception:  # noqa: BLE001 - best-effort cleanup
            leftovers.append(f"tag ref {inputs.tag} (cleanup state unknown)")

    # Resolve the release id even when create_release was ambiguous (no id
    # returned to the caller): look the draft up by tag_name from live state.
    release_id = result.release_id
    if not release_id:
        try:
            release_id = budget.call_with_timeout(lambda: _draft_id_for_tag(client, inputs.tag)) or 0
        except Exception:  # noqa: BLE001 - best-effort cleanup
            # The server may have created a draft we could not look up; record
            # it so an operator deletes it by hand.
            release_id = 0
            leftovers.append(f"draft release for tag {inputs.tag} (lookup failed; delete by hand if present)")

    # Release: delete only if still a draft.
    if release_id:
        try:
            still_draft = budget.call_with_timeout(lambda: _release_is_draft(client, release_id))
            if still_draft:
                budget.call_with_timeout(lambda: client.delete_release(release_id))
            elif still_draft is False:
                leftovers.append(f"published release id={release_id} (left intact)")
        except Exception:  # noqa: BLE001 - best-effort cleanup
            leftovers.append(f"release id={release_id} (cleanup state unknown)")

    if leftovers:
        print(
            "release cleanup could not remove everything; manual recovery required for: "
            + "; ".join(leftovers)
            + " (see docs/RELEASING.md 'Manual recovery')",
            file=sys.stderr,
        )
    else:
        # Everything this run announced was confirmed removed. Printing a
        # positive line makes the three outcomes distinguishable to an operator:
        # announcement + this line => nothing left; announcement +
        # manual-recovery line => the listed items; announcement alone => the
        # process was killed mid-cleanup, so verify every announced identifier.
        print(
            "release cleanup complete; nothing left to remove",
            file=sys.stderr,
        )


def _draft_id_for_tag(client: GitHubClient, tag: str) -> int | None:
    """Return the id of a draft release using ``tag``, or None.

    Used by cleanup when ``create_release`` was ambiguous (the server may have
    created the draft while the client saw an error), so no id reached the
    caller.
    """

    for release in client.list_releases():
        if str(release.get("tag_name")) == tag and bool(release.get("draft", False)):
            return int(release.get("id", 0)) or None
    return None


def _release_is_draft(client: GitHubClient, release_id: int) -> bool | None:
    """Return True/False if the release's draft state is known, else None."""

    for release in client.list_releases():
        if int(release.get("id", -1)) == int(release_id):
            return bool(release.get("draft", False))
    return None


def _published_release_exists(client: GitHubClient, tag: str) -> bool:
    for release in client.list_releases():
        if str(release.get("tag_name")) == tag and not release.get("draft", True):
            return True
    return False


def load_artifacts_from_dir(artifact_dir: Path) -> list[Artifact]:
    """Load exactly the allowlisted artifact set from a downloaded directory.

    The ``publish`` job calls this on the directory ``actions/download-artifact``
    populated from the ``build-artifacts`` job. Only the four allowlisted names
    are read; a missing or extra file is rejected so the published set is
    exactly the built set.
    """

    present = {p.name for p in artifact_dir.iterdir() if p.is_file()}
    missing = ALLOWED_ARTIFACT_NAMES - present
    if missing:
        raise ValidationError(f"downloaded artifact set is missing {sorted(missing)}")
    extra = present - ALLOWED_ARTIFACT_NAMES
    if extra:
        raise ValidationError(f"downloaded artifact set has unexpected files {sorted(extra)}")

    artifacts = [
        Artifact(name=name, data=(artifact_dir / name).read_bytes(), content_type=_content_type_for(name))
        for name in sorted(ALLOWED_ARTIFACT_NAMES)
    ]
    return artifacts


def _verify_manifest_matches_inputs(artifacts: list[Artifact], metadata: PublishMetadata) -> None:
    """Verify the downloaded manifest's identity equals the trusted inputs.

    The manifest's ``tag``, ``commit``, and ``attached_files`` must equal the
    dispatch inputs and the actual downloaded file set. The downloaded bytes are
    never trusted to assert their own identity; they are checked against the
    server-owned dispatch facts.
    """

    manifest_bytes = next((a.data for a in artifacts if a.name == MANIFEST_NAME), None)
    _require(manifest_bytes is not None, "downloaded set has no release manifest")
    try:
        manifest = json.loads((manifest_bytes or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("release manifest is not valid UTF-8 JSON") from error
    _require(isinstance(manifest, dict), "release manifest is not a JSON object")

    _require(str(manifest.get("tag", "")) == metadata.tag, "manifest tag does not equal the dispatched tag")
    _require(str(manifest.get("commit", "")) == metadata.commit, "manifest commit does not equal the dispatched commit")

    manifest_files = manifest.get("attached_files")
    _require(isinstance(manifest_files, list), "manifest attached_files is not a list")
    _require(
        sorted(str(name) for name in manifest_files) == sorted(ALLOWED_ARTIFACT_NAMES),
        "manifest attached_files does not equal the downloaded artifact set",
    )


def publish_release_from_dir(
    client: GitHubClient,
    artifact_dir: Path,
    metadata: PublishMetadata,
    *,
    rebuilt_manifest: bytes | None = None,
) -> PublishResult:
    """Publish exactly the verified, downloaded artifact set.

    This is the ``publish`` job's single entrypoint. It performs no artifact
    generation: it loads the downloaded set, verifies it against ``SHA256SUMS``
    and the exclusion guard, verifies the manifest identity equals the trusted
    inputs, and -- when ``rebuilt_manifest`` is supplied -- requires the
    downloaded manifest to be byte-identical to a manifest rebuilt from the
    publish-time checkout and re-validation result (closing the approval-window
    tamper gap). It then runs the ordered publication (which re-runs the live
    pre-write validations and the environment-protection check).
    """

    artifacts = load_artifacts_from_dir(artifact_dir)
    sums = next(a.data for a in artifacts if a.name == CHECKSUMS_NAME)

    # Tamper check and public-safety guard before trusting any manifest field.
    assert_artifacts_safe(artifacts)
    covered = {a.name: a.data for a in artifacts if a.name != CHECKSUMS_NAME}
    checksums.verify_against_sums(sums, covered)

    # The manifest's self-described identity must equal the dispatch inputs.
    _verify_manifest_matches_inputs(artifacts, metadata)

    # Re-bind the full manifest content: the downloaded manifest must equal the
    # one rebuilt at publish time from the checkout and re-validation result.
    if rebuilt_manifest is not None:
        downloaded_manifest = next(a.data for a in artifacts if a.name == MANIFEST_NAME)
        assert_manifest_byte_identical(downloaded_manifest=downloaded_manifest, rebuilt_manifest=rebuilt_manifest)

    # Fail closed unless the deployment environment is protected: a non-dry run
    # must never publish into an environment GitHub would auto-create
    # unprotected on first use. (The workflow also runs this as a dedicated
    # read-only step before this one; both are intentional defense in depth.)
    validate_environment_protected(client)

    inputs = PublishInputs(
        tag=metadata.tag,
        commit=metadata.commit,
        release_name=metadata.release_name,
        notes=metadata.notes,
        artifacts=artifacts,
        sha256sums=sums,
        tagger_name=metadata.tagger_name,
        tagger_email=metadata.tagger_email,
        commit_date=metadata.commit_date,
    )
    return publish_release(client, inputs)
