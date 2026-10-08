"""Static-contract tests for .github/workflows/release.yml.

String-structural style (no PyYAML) matching the existing workflow tests. These
assert the security-critical shape of the release workflow directly against its
source text.
"""

# Standard library
import os
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
TEST_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "test.yml"


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _split_jobs(text: str) -> dict[str, str]:
    """Return {job_id: job_block_text} by indentation under ``jobs:``.

    A job is a 2-space-indented key under the top-level ``jobs:`` mapping; its
    block runs until the next 2-space-indented key or EOF.
    """

    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^jobs:\s*$", line):
            start = i + 1
            break
    assert start is not None, "workflow has no top-level jobs:"
    jobs: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in lines[start:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            if current is not None:
                jobs[current] = "".join(buf)
            current = m.group(1)
            buf = [line]
        else:
            if re.match(r"^[A-Za-z]", line) and current is not None:
                break
            buf.append(line)
    if current is not None:
        jobs[current] = "".join(buf)
    return jobs


class ReleaseWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW_PATH.is_file(), "release.yml must exist")
        self.text = _workflow_text()

    def test_dispatch_only_trigger(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotRegex(self.text, r"\n\s*push:")
        self.assertNotRegex(self.text, r"\n\s*pull_request:")
        self.assertNotRegex(self.text, r"\n\s*schedule:")

    def test_declares_all_inputs(self):
        for name in ["commit:", "tag:", "evidence_comment_url:", "dry_run:"]:
            self.assertIn(name, self.text)

    def test_dry_run_defaults_true(self):
        # The dry_run input must default to true (safe default).
        block = self.text.split("dry_run:", 1)[1][:400]
        self.assertRegex(block, r"default:\s*true")
        self.assertRegex(block, r"type:\s*boolean")

    def test_top_level_permissions_empty(self):
        self.assertRegex(self.text, r"\npermissions:\s*\{\}\s*\n")

    def test_no_id_token(self):
        self.assertNotIn("id-token", self.text)

    def test_no_aws_usage(self):
        lowered = self.text.lower()
        self.assertNotIn("aws-actions/", lowered)
        self.assertNotIn("configure-aws-credentials", lowered)
        self.assertNotIn("secrets.aws", lowered)
        # No `aws` CLI invocation. Prose mentions of "AWS" are fine; an
        # executable `aws <service>` call (optionally after `run:` or a pipe)
        # is not. Match an `aws` command token at a command position.
        self.assertNotRegex(
            self.text,
            r"(?m)(?:^|run:\s*|[|&;]\s*)aws\s+[a-z0-9-]+",
            msg="aws CLI invocation found in workflow",
        )

    def test_only_github_hosted_runners(self):
        runs_on = re.findall(r"runs-on:\s*(.+)", self.text)
        self.assertTrue(runs_on)
        for value in runs_on:
            self.assertIn("ubuntu-latest", value)
        self.assertNotIn("self-hosted", self.text)
        self.assertNotIn("runner-group", self.text)

    def test_validation_job_least_privilege(self):
        # The validation job may only read contents/checks/issues.
        self.assertIn("contents: read", self.text)
        self.assertIn("checks: read", self.text)
        self.assertIn("issues: read", self.text)

    def test_publish_job_contents_write_only(self):
        self.assertIn("contents: write", self.text)

    def test_checkout_does_not_persist_credentials(self):
        self.assertIn("persist-credentials: false", self.text)

    def test_third_party_actions_pinned_to_sha(self):
        # Every `uses:` that is not a first-party actions/* or github/* action
        # must be pinned to a 40-hex commit sha.
        for match in re.finditer(r"uses:\s*([^\s]+)", self.text):
            ref = match.group(1)
            owner = ref.split("/", 1)[0]
            if owner in ("actions", "github"):
                continue
            self.assertRegex(ref, r"@[0-9a-f]{40}\b", msg=f"unpinned action: {ref}")

    def test_inputs_reach_scripts_via_env_not_interpolation(self):
        # No `${{ github.event.inputs.* }}` or `${{ inputs.* }}` inside a run:
        # command line (they must be surfaced through env:).
        run_lines = [
            line for line in self.text.splitlines() if re.search(r"\$\{\{\s*(?:github\.event\.)?inputs\.", line)
        ]
        for line in run_lines:
            # Allowed only on an `env:`-assignment line or within the inputs block.
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("run:") or stripped.startswith("- run:"),
                msg=f"input interpolated into run: {line}",
            )

    def test_concurrency_group_no_cancel(self):
        self.assertIn("concurrency:", self.text)
        self.assertIn("cancel-in-progress: false", self.text)

    def test_publish_gated_on_dry_run_false(self):
        # Some job/step condition must gate publication on dry_run == false.
        self.assertRegex(
            self.text, r"inputs\.dry_run\s*==\s*false|!\s*fromJSON\(inputs\.dry_run\)|inputs\.dry_run\s*!=\s*'?true"
        )

    def test_no_source_archive_upload(self):
        lowered = self.text.lower()
        self.assertNotIn("zipball", lowered)
        self.assertNotIn("tarball", lowered)
        self.assertNotIn("source.zip", lowered)

    def test_only_default_token_secret(self):
        # No custom secrets. Only github.token / secrets.GITHUB_TOKEN permitted.
        for match in re.finditer(r"secrets\.([A-Za-z0-9_]+)", self.text):
            self.assertEqual(match.group(1), "GITHUB_TOKEN", msg=f"unexpected secret: {match.group(1)}")


class TokenScopingContractTests(unittest.TestCase):
    """A write token must never sit in a job-level env, and the write-scoped
    publish job must run only first-party actions and no SBOM regeneration."""

    def setUp(self):
        self.assertTrue(WORKFLOW_PATH.is_file(), "release.yml must exist")
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def _job_level_env_block(self, block: str) -> str:
        """Extract the 4-space-indented ``env:`` block directly under a job."""
        lines = block.splitlines(keepends=True)
        out: list[str] = []
        in_env = False
        for line in lines:
            if re.match(r"^    env:\s*$", line):
                in_env = True
                continue
            if in_env:
                # env entries are 6-space indented; anything shallower ends it.
                if re.match(r"^      \S", line):
                    out.append(line)
                    continue
                if line.strip() == "":
                    continue
                in_env = False
        return "".join(out)

    def test_no_token_in_any_job_level_env(self):
        for job_id, block in self.jobs.items():
            job_env = self._job_level_env_block(block)
            self.assertNotRegex(
                job_env,
                r"(?m)^\s*(?:GITHUB_TOKEN|GH_TOKEN)\s*:",
                msg=f"job {job_id} exposes a token in job-level env",
            )

    def test_publish_uses_only_first_party_actions(self):
        publish = self.jobs.get("publish", "")
        self.assertTrue(publish, "publish job must exist")
        for match in re.finditer(r"uses:\s*([^\s]+)", publish):
            ref = match.group(1)
            owner = ref.split("/", 1)[0]
            self.assertEqual(owner, "actions", msg=f"publish uses a non-first-party action: {ref}")

    def test_publish_does_not_run_syft_or_regenerate_sboms(self):
        publish = self.jobs.get("publish", "")
        lowered = publish.lower()
        self.assertNotIn("syft", lowered, "publish must not run syft")
        self.assertNotIn("download-syft", lowered, "publish must not install syft")
        # No SBOM *generation* command. Downloading the artifact set is fine.
        self.assertNotRegex(publish, r"spdx-json=", msg="publish must not generate SBOMs")

    def test_publish_downloads_the_verified_artifact(self):
        publish = self.jobs.get("publish", "")
        self.assertRegex(publish, r"download-artifact", msg="publish must download the built artifact set")
        self.assertIn("release-artifacts", publish)

    def test_build_artifacts_token_only_at_step_scope(self):
        # The read-only build-artifacts job must not carry the token at job
        # scope either; it belongs only on the Python step that needs it.
        build = self.jobs.get("build-artifacts", "")
        self.assertTrue(build, "build-artifacts job must exist")
        env_block = self._job_level_env_block(build)
        self.assertNotRegex(env_block, r"(?m)^\s*GITHUB_TOKEN\s*:")


class EnvironmentProtectionContractTests(unittest.TestCase):
    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def test_publish_declares_release_environment(self):
        publish = self.jobs.get("publish", "")
        # Anchor to the YAML key line of the publish job, so a matching comment
        # (e.g. prose mentioning `environment: release`) cannot satisfy this.
        self.assertRegex(
            publish,
            r"(?m)^    environment:\s*release\s*$|^    environment:\s*\n^      name:\s*release\s*$",
            msg="publish must declare `environment: release` as a job key, not merely in a comment",
        )

    def test_validate_runs_the_environment_protection_check(self):
        # The validate job must run `release.cli verify-environment` so a dry
        # run fails closed on a missing or unprotected environment. Removing
        # that step must fail here. (This asserts the step is present in the
        # validate job, not its position among the steps.)
        validate_block = self.jobs.get("validate", "")
        self.assertIn(
            "release.cli verify-environment",
            validate_block,
            msg="validate must run verify-environment",
        )

    def test_non_dry_run_verifies_environment_protection_before_write(self):
        # The publish path must invoke the environment-protection check
        # (verify-environment) and hold actions: read to call the environments
        # REST endpoint.
        publish = self.jobs.get("publish", "")
        self.assertIn("verify-environment", publish, "publish must verify environment protection before writing")
        self.assertIn("actions: read", publish, "the environment check needs actions: read")


class DispatchRefGuardContractTests(unittest.TestCase):
    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def test_every_job_guarded_on_main_ref(self):
        # Each job must only run when the dispatch ref is refs/heads/main; the
        # workflow file itself comes from the dispatch ref, so the scripts'
        # commit==main check is not sufficient on its own.
        for job_id, block in self.jobs.items():
            self.assertRegex(
                block,
                r"github\.ref\s*==\s*'refs/heads/main'",
                msg=f"job {job_id} is not guarded on the main dispatch ref",
            )


class ActionPinContractTests(unittest.TestCase):
    def setUp(self):
        self.text = _workflow_text()

    def test_every_uses_is_sha_pinned_with_version_comment(self):
        # Every `uses:` must be `owner/name@<40-hex> # vX.Y.Z`.
        uses_lines = [ln for ln in self.text.splitlines() if re.search(r"\buses:", ln)]
        self.assertTrue(uses_lines, "workflow must declare at least one action")
        for line in uses_lines:
            m = re.search(r"uses:\s*(\S+)@([0-9a-f]{40})\b\s*#\s*v\d+\.\d+\.\d+", line)
            self.assertIsNotNone(m, msg=f"not SHA-pinned with version comment: {line.strip()}")

    def test_core_action_majors_match_test_workflow(self):
        # checkout / upload-artifact / download-artifact majors in release.yml
        # must equal those used by .github/workflows/test.yml.
        test_text = TEST_WORKFLOW_PATH.read_text(encoding="utf-8")

        for name in ("actions/checkout", "actions/upload-artifact", "actions/download-artifact"):
            # Majors declared in test.yml (floating @vN form).
            test_majors = {m.group(1) for m in re.finditer(rf"{re.escape(name)}@v(\d+)", test_text)}
            # Majors declared in release.yml (SHA-pinned, major from the comment).
            rel_majors = {m.group(1) for m in re.finditer(rf"{re.escape(name)}@[0-9a-f]{{40}}\s*#\s*v(\d+)", self.text)}
            if name == "actions/download-artifact" and not rel_majors:
                self.fail("release.yml must use actions/download-artifact (SHA-pinned)")
            if rel_majors:
                self.assertTrue(
                    rel_majors.issubset(test_majors) or test_majors.issubset(rel_majors) or rel_majors == test_majors,
                    msg=f"{name}: release majors {rel_majors} != test majors {test_majors}",
                )


class RunBlockInterpolationContractTests(unittest.TestCase):
    """No dispatch input may be interpolated anywhere inside a run block.

    A ``${{ inputs.* }}`` reference must never appear in a ``run:`` command
    line, including a continuation line of a ``run: |`` block, so this scans the
    full body of every ``run:`` block rather than only its first line.
    """

    def setUp(self):
        self.text = _workflow_text()

    def _run_blocks(self) -> list[str]:
        """Return the text of every ``run:`` block (inline or ``|`` multi-line)."""
        lines = self.text.splitlines(keepends=True)
        blocks: list[str] = []
        i = 0
        while i < len(lines):
            m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[i])
            if not m:
                i += 1
                continue
            indent = len(m.group(1))
            rest = m.group(2).rstrip("\n")
            buf = [rest]
            # A block scalar (| or >) continues while more-indented.
            if rest.strip() in ("|", ">", "|-", ">-", "|+", ">+"):
                j = i + 1
                while j < len(lines):
                    line = lines[j]
                    if line.strip() == "":
                        buf.append(line)
                        j += 1
                        continue
                    cur_indent = len(line) - len(line.lstrip(" "))
                    if cur_indent <= indent:
                        break
                    buf.append(line)
                    j += 1
                i = j
            else:
                i += 1
            blocks.append("".join(buf))
        return blocks

    def test_no_dispatch_input_interpolated_in_any_run_block(self):
        blocks = self._run_blocks()
        self.assertTrue(blocks, "workflow must have at least one run block")
        pattern = re.compile(r"\$\{\{\s*(?:github\.event\.)?inputs\.")
        for block in blocks:
            self.assertNotRegex(
                block,
                pattern,
                msg=f"dispatch input interpolated into a run block:\n{block}",
            )


class PerJobPermissionContractTests(unittest.TestCase):
    """Discriminate per-job permission sets, not just a substring anywhere."""

    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def _permissions_block(self, block: str) -> dict[str, str]:
        """Parse the 4-space ``permissions:`` block of a job into {scope: level}."""
        lines = block.splitlines()
        perms: dict[str, str] = {}
        in_perms = False
        for line in lines:
            if re.match(r"^    permissions:\s*$", line):
                in_perms = True
                continue
            if in_perms:
                m = re.match(r"^      ([a-z-]+):\s*(\S+)", line)
                if m:
                    perms[m.group(1)] = m.group(2)
                    continue
                if line.strip() == "" or line.strip().startswith("#"):
                    continue
                in_perms = False
        return perms

    def test_build_artifacts_is_read_only(self):
        # The job that runs third-party syft must never hold contents: write.
        perms = self._permissions_block(self.jobs["build-artifacts"])
        self.assertEqual(perms.get("contents"), "read", msg=f"build-artifacts perms: {perms}")
        self.assertNotIn("write", set(perms.values()))

    def test_validate_is_read_only(self):
        # The validate job must never hold a write scope.
        perms = self._permissions_block(self.jobs["validate"])
        self.assertEqual(perms.get("contents"), "read", msg=f"validate perms: {perms}")
        self.assertNotIn("write", set(perms.values()))

    def test_publish_has_checks_and_issues_read(self):
        # publish performs check-run and issue-comment reads.
        perms = self._permissions_block(self.jobs["publish"])
        self.assertEqual(perms.get("contents"), "write")
        self.assertEqual(perms.get("checks"), "read")
        self.assertEqual(perms.get("issues"), "read")
        self.assertEqual(perms.get("actions"), "read")

    def test_every_permission_has_a_justification_comment(self):
        # Each permission entry carries an inline justification comment.
        for job_id in ("validate", "build-artifacts", "publish"):
            block = self.jobs[job_id]
            in_perms = False
            for line in block.splitlines():
                if re.match(r"^    permissions:\s*$", line):
                    in_perms = True
                    continue
                if in_perms:
                    m = re.match(r"^      ([a-z-]+):\s*(\S+)(.*)$", line)
                    if m:
                        self.assertIn("#", m.group(3), msg=f"{job_id}:{m.group(1)} lacks a justification comment")
                        continue
                    if line.strip() == "" or line.strip().startswith("#"):
                        continue
                    in_perms = False


class CommitEqualsShaGuardContractTests(unittest.TestCase):
    """Every job must bind inputs.commit to github.sha.

    build-artifacts and publish bind it in their job-level ``if:``; validate
    binds it in an explicit pre-checkout shell guard that fails loudly (its
    ``if:`` deliberately omits the clause so the guard stays reachable on a
    stale SHA).
    """

    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def test_every_job_requires_commit_equals_github_sha(self):
        for job_id, block in self.jobs.items():
            if job_id == "validate":
                # validate binds the commit in a shell guard (inputs.commit is
                # surfaced as GBAW_RELEASE_COMMIT and compared to GITHUB_SHA),
                # not in its job-level if:.
                self.assertIn("GBAW_RELEASE_COMMIT: ${{ inputs.commit }}", block)
                self.assertRegex(
                    block,
                    r'\[\s*"\$GBAW_RELEASE_COMMIT"\s*(?:=|==|!=)\s*"\$GITHUB_SHA"\s*\]',
                    msg="validate must compare the dispatched commit to GITHUB_SHA in shell",
                )
                continue
            self.assertRegex(
                block,
                r"inputs\.commit\s*==\s*github\.sha",
                msg=f"job {job_id} does not bind inputs.commit to github.sha",
            )


class StepLevelTokenContractTests(unittest.TestCase):
    """The token must appear only on Python steps that call the API."""

    def setUp(self):
        self.text = _workflow_text()

    def test_token_env_only_adjacent_to_python_release_cli(self):
        # Every ``GITHUB_TOKEN:`` step-env line must belong to a step whose
        # ``run:`` invokes ``release.cli`` (never a syft/mkdir shell step).
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^\s*GITHUB_TOKEN:\s*\$\{\{\s*github\.token\s*\}\}\s*$", line):
                window = "\n".join(lines[i : i + 8])
                self.assertRegex(
                    window,
                    r"run:\s*(?:exec )?python3 -m release\.cli",
                    msg=f"GITHUB_TOKEN near a non-release.cli step at line {i + 1}",
                )

    def test_syft_steps_do_not_receive_token(self):
        # The two syft generation steps must not carry GITHUB_TOKEN in env.
        for marker in ("Generate source SBOMs (build 1)", "Generate source SBOMs (build 2)"):
            start = self.text.index(marker)
            # Scan until the next step (``- name:``) or EOF.
            rest = self.text[start:]
            next_step = rest.find("\n      - name:", 1)
            block = rest if next_step == -1 else rest[:next_step]
            self.assertNotIn("GITHUB_TOKEN", block, msg=f"{marker} received a token")


class TriggerAllowlistContractTests(unittest.TestCase):
    """Only workflow_dispatch may trigger the workflow."""

    def setUp(self):
        self.text = _workflow_text()

    def test_only_workflow_dispatch_trigger(self):
        on_block = self.text.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        triggers = re.findall(r"^  ([a-z_]+):", on_block, flags=re.MULTILINE)
        self.assertEqual(triggers, ["workflow_dispatch"], msg=f"unexpected triggers: {triggers}")


class PerCheckoutPersistCredentialsContractTests(unittest.TestCase):
    """EVERY checkout step must set persist-credentials: false."""

    def setUp(self):
        self.text = _workflow_text()

    def test_every_checkout_disables_credential_persistence(self):
        checkout_count = len(re.findall(r"uses:\s*actions/checkout@", self.text))
        self.assertGreaterEqual(checkout_count, 3)
        persist_count = len(re.findall(r"^\s*persist-credentials: false\s*$", self.text, flags=re.MULTILINE))
        self.assertEqual(
            persist_count,
            checkout_count,
            msg=f"{checkout_count} checkouts but {persist_count} persist-credentials:false",
        )


class ExactJobConditionContractTests(unittest.TestCase):
    """Pin each job's exact ``if:`` expression so an && -> || swap fails.

    A regex that only asserts the presence of ``inputs.commit == github.sha``
    passes when the operator is changed to ``||``; these assert the full
    expression text instead.
    """

    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def _if_expression(self, job_id: str) -> str:
        block = self.jobs[job_id]
        m = re.search(r"^\s*if:\s*(.+?)\s*$", block, flags=re.MULTILINE)
        self.assertIsNotNone(m, msg=f"job {job_id} has no if:")
        return m.group(1).strip()

    def test_validate_exact_condition(self):
        self.assertEqual(
            self._if_expression("validate"),
            "${{ github.ref == 'refs/heads/main' }}",
        )

    def test_build_artifacts_exact_condition(self):
        self.assertEqual(
            self._if_expression("build-artifacts"),
            "${{ github.ref == 'refs/heads/main' && inputs.commit == github.sha }}",
        )

    def test_publish_exact_condition(self):
        self.assertEqual(
            self._if_expression("publish"),
            "${{ github.ref == 'refs/heads/main' && inputs.commit == github.sha && inputs.dry_run == false }}",
        )


class CommitShellGuardContractTests(unittest.TestCase):
    """The validate job must compare the dispatched commit to github.sha in a
    shell step BEFORE checkout, failing with an ``::error::`` naming the
    expected SHA, so a stale/mistyped SHA fails loudly instead of skipping all
    jobs silently.

    The guard is only reachable because validate's job-level ``if:`` does NOT
    itself require ``inputs.commit == github.sha`` (a false job condition would
    skip the job before any step runs). The guard must NEVER interpolate the
    dispatched value into its output: the ``::error::`` line names only
    $GITHUB_SHA (the trusted, server-owned main HEAD), so a value dispatched
    through the REST API cannot inject a workflow command.
    """

    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _split_jobs(self.text)

    def _if_expression(self, job_id: str) -> str:
        block = self.jobs[job_id]
        m = re.search(r"^\s*if:\s*(.+?)\s*$", block, flags=re.MULTILINE)
        assert m is not None, f"job {job_id} has no if:"
        return m.group(1).strip()

    def _guard_run_text(self) -> str:
        """Return the exact ``run:`` body of the commit-compare guard step.

        Extracted from the validate job so the behavioral test executes the real
        shell the runner would, not a hand-copied approximation.
        """
        validate_block = self.jobs["validate"]
        lines = validate_block.splitlines(keepends=True)
        # Find the guard step's `run: |` and collect its indented block body.
        run_idx = None
        run_indent = 0
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)run:\s*\|\s*$", line)
            if m and "$GBAW_RELEASE_COMMIT" in "".join(lines[i : i + 8]):
                run_idx = i
                run_indent = len(m.group(1))
                break
        assert run_idx is not None, "could not locate the guard run: block"
        body: list[str] = []
        for line in lines[run_idx + 1 :]:
            if line.strip() == "":
                body.append("\n")
                continue
            cur_indent = len(line) - len(line.lstrip(" "))
            if cur_indent <= run_indent:
                break
            body.append(line)
        return textwrap.dedent("".join(body))

    def test_validate_job_if_does_not_gate_on_commit_so_guard_is_reachable(self):
        # If validate's job condition required inputs.commit == github.sha, the
        # job (and the guard step) would be skipped silently on a stale SHA.
        self.assertNotIn("inputs.commit", self._if_expression("validate"))

    def test_validate_has_pre_checkout_commit_compare_step(self):
        validate_block = self.jobs["validate"]
        steps = validate_block.split("- name:")
        # Find the checkout step index and a commit-compare step index.
        checkout_idx = next((i for i, s in enumerate(steps) if "actions/checkout@" in s), None)
        compare_idx = next((i for i, s in enumerate(steps) if "GBAW_RELEASE_COMMIT" in s and "GITHUB_SHA" in s), None)
        self.assertIsNotNone(checkout_idx, msg="validate has no checkout step")
        self.assertIsNotNone(compare_idx, msg="validate has no commit-compare step")
        self.assertLess(compare_idx, checkout_idx, msg="commit-compare step must run before checkout")

    def test_commit_compare_step_errors_with_expected_sha(self):
        validate_block = self.jobs["validate"]
        self.assertRegex(
            validate_block,
            r'\[\s*"\$GBAW_RELEASE_COMMIT"\s*(?:=|==|!=)\s*"\$GITHUB_SHA"\s*\]',
            msg="validate must compare GBAW_RELEASE_COMMIT to GITHUB_SHA in shell",
        )
        self.assertIn("::error::", validate_block, msg="commit mismatch must emit ::error::")

    def test_guard_never_interpolates_the_dispatched_commit(self):
        # The guard must not interpolate $GBAW_RELEASE_COMMIT anywhere in its
        # output; the only variable it echoes is the trusted $GITHUB_SHA. The
        # shape-check branch (which previously echoed the input) must be gone.
        validate_block = self.jobs["validate"]
        for m in re.finditer(r"echo[^\n]*\$GBAW_RELEASE_COMMIT", validate_block):
            self.fail(f"guard echoes the dispatched commit: {m.group(0)!r}")
        # The single ::error:: line names only $GITHUB_SHA.
        self.assertRegex(
            validate_block,
            r"::error::dispatched commit does not equal main HEAD \$GITHUB_SHA",
            msg="the mismatch error must name only $GITHUB_SHA",
        )

    def test_guard_output_cannot_be_injected_by_a_dispatched_newline_payload(self):
        # Execute the guard's exact run: text under bash -e with a synthetic
        # GITHUB_SHA. An equal commit exits 0 with no output; every other value,
        # including multi-line payloads that try to smuggle a second workflow
        # command, must exit 1 and print exactly one line that starts with
        # ::error::, names neither the payload nor any second command marker.
        if shutil.which("bash") is None:
            self.skipTest("bash is unavailable")
        guard = self._guard_run_text()
        good_sha = "0123456789abcdef0123456789abcdef01234567"
        stale_sha = "fedcba9876543210fedcba9876543210fedcba98"

        def run(commit_value: str):
            proc = subprocess.run(
                ["bash", "-e", "-c", guard],
                env={**os.environ, "GBAW_RELEASE_COMMIT": commit_value, "GITHUB_SHA": good_sha},
                capture_output=True,
                text=True,
            )
            return proc

        # Equal: exit 0, nothing printed.
        ok = run(good_sha)
        self.assertEqual(ok.returncode, 0, msg=f"equal SHA should pass: {ok.stdout!r} {ok.stderr!r}")
        self.assertEqual(ok.stdout.strip(), "", msg=f"equal SHA should print nothing: {ok.stdout!r}")

        payloads = [
            stale_sha,  # a plain stale value
            f"{good_sha}\n::warning title=Release approved::forged",  # LF-separated injection
            f"{good_sha}\r::add-mask::forged",  # CR-separated injection
            f"{good_sha}\r\n::warning::forged",  # CRLF-separated injection
            f"{good_sha}\n  ::warning::indented",  # indented command (runner trims)
            f"{good_sha}\n::add-mask::secret",  # add-mask injection
            f"{good_sha}\n##[error]legacy",  # mid/leading legacy V1 command
        ]
        for payload in payloads:
            proc = run(payload)
            self.assertEqual(proc.returncode, 1, msg=f"payload {payload!r} must fail: {proc.stdout!r}")
            # Split stdout the way the runner does: on LF, CR, or CRLF.
            raw_lines = re.split(r"\r\n|\r|\n", proc.stdout)
            lines = [ln for ln in raw_lines if ln.strip() != ""]
            self.assertEqual(
                len(lines),
                1,
                msg=f"payload {payload!r} produced more than one output line: {proc.stdout!r}",
            )
            only = lines[0]
            self.assertTrue(only.startswith("::error::"), msg=f"line must be an ::error::: {only!r}")
            # The line must not echo the dispatched value or any second command.
            self.assertNotIn(payload, only, msg=f"guard echoed the payload: {only!r}")
            self.assertNotIn("::warning", only, msg=f"guard emitted a second command: {only!r}")
            self.assertNotIn("::add-mask", only, msg=f"guard emitted a second command: {only!r}")
            self.assertNotIn("##[", only, msg=f"guard emitted a legacy command: {only!r}")


if __name__ == "__main__":
    unittest.main()
