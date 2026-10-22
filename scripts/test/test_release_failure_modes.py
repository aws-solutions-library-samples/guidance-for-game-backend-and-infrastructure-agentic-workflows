"""Failure-mode regression tests for the milestone release tooling.

These tests exercise the ways the release tooling must behave safely under
edge and failure conditions: the public-content guard must not misfire on real
syft SBOM and checksum artifacts, required-check validation must read the live
branch ruleset and fail closed on skipped/missing/stale runs, SBOM
normalization must emit a UTC-Z instant and a per-release namespace, publish
must rebind the manifest and clean up robustly under interruption or ambiguous
API responses, the operations-mode proof must fail closed on any non-doc
reference, and evidence binding and waiver rules must reject tampered or
over-broad inputs.

Each test asserts the observable behavior, not import or fixture setup, so it
would fail against tooling that lacked the safeguard. Standard library only.
"""

# Standard library
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# Third-party packages
from _release_fakes import RecordingClient, completed_check  # noqa: E402

# Local modules
from release import artifact_guard, manifest, publish, sbom, validate  # noqa: E402
from release.artifact_guard import Artifact  # noqa: E402

COMMIT = "a" * 40


# ---------------------------------------------------------------------------
# The public-safety guard must not reject real syft SBOMs / SHA256SUMS.
# ---------------------------------------------------------------------------
class GuardAlphanumericBordersTests(unittest.TestCase):
    """A 12-digit decimal run bordered by hex letters is NOT an account id.

    syft names every package ``SPDXRef-...-<16 hex>`` and SBOM/SHA256SUMS carry
    40- and 64-hex digests; any of these can contain a 12-decimal-digit run
    between hex letters. The guard must use the repository scanner's
    alphanumeric borders so it does not fire on them.
    """

    def test_hex_bordered_12_digit_run_is_not_account_id(self):
        # A realistic syft SPDXID: letters on both sides of a 12-digit run.
        payload = b'{"SPDXID": "SPDXRef-Package-python-certifi-a338775240494b9f"}\n'
        violations = artifact_guard.scan_content(artifact_guard.BACKEND_SBOM_NAME, payload)
        self.assertNotIn("account-id", violations, msg=f"hex-bordered digits flagged: {violations}")

    def test_sha256_digest_line_is_not_account_id(self):
        # A SHA256SUMS line: 64 hex chars then two spaces then a name.
        digest = "7c9b2f00112233445566b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2"
        line = f"{digest}  release-manifest.json\n".encode()
        violations = artifact_guard.scan_content(artifact_guard.CHECKSUMS_NAME, line)
        self.assertNotIn("account-id", violations, msg=f"sha256 digest flagged: {violations}")

    def test_nonsynthetic_account_id_still_rejected(self):
        hostile = ("9" * 12).encode()
        payload = b'{"a": "' + hostile + b'"}\n'
        violations = artifact_guard.scan_content(artifact_guard.MANIFEST_NAME, payload)
        self.assertIn("account-id", violations)

    def test_account_id_inside_arn_still_rejected(self):
        # Build the hostile 12-digit account id at runtime (never a literal).
        hostile = ("4" * 4 + "5" * 4 + "6" * 4).encode()
        payload = b'{"role": "arn:aws:iam::' + hostile + b':role/x"}\n'
        violations = artifact_guard.scan_content(artifact_guard.MANIFEST_NAME, payload)
        self.assertIn("account-id", violations)

    def test_guard_reuses_repository_scanner_pattern(self):
        # The guard must not keep a private copy of the account-id regex; it
        # must reuse the repository scanner's compiled pattern.
        # Local modules
        import check_public_content as scanner  # noqa: E402

        self.assertIs(artifact_guard._ACCOUNT_ID_RE, scanner.ACCOUNT_ID_PATTERN)


# ---------------------------------------------------------------------------
# Required-check selection from the live ruleset, skipped-aware.
# ---------------------------------------------------------------------------
def _rules_payload(contexts):
    return [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": c, "integration_id": 15368} for c in contexts],
                "strict_required_status_checks_policy": True,
            },
        }
    ]


def _checks_ready_client(commit=COMMIT):
    client = RecordingClient()
    client.main_head = commit
    client.branch_rules = _rules_payload(validate.REQUIRED_CHECK_NAMES)
    for name in validate.REQUIRED_CHECK_NAMES:
        client.check_runs[name] = [completed_check(name, commit)]
    return client


class RequiredChecksFromRulesetTests(unittest.TestCase):
    def test_contexts_read_from_branch_ruleset(self):
        client = _checks_ready_client()
        # Ruleset requires only two contexts; both green -> passes even though
        # the hardcoded list has seven.
        client.branch_rules = _rules_payload(["backend-tests", "frontend-tests"])
        client.check_runs = {
            "backend-tests": [completed_check("backend-tests", COMMIT)],
            "frontend-tests": [completed_check("frontend-tests", COMMIT)],
        }
        conclusions = validate.validate_required_checks(client, COMMIT)
        self.assertEqual({c.name for c in conclusions}, {"backend-tests", "frontend-tests"})

    def test_renamed_required_context_is_enforced(self):
        client = _checks_ready_client()
        client.branch_rules = _rules_payload(["backend-tests", "a-new-required-check"])
        client.check_runs = {"backend-tests": [completed_check("backend-tests", COMMIT)]}
        # The new required context has no run -> must fail (not silently pass).
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_skipped_scheduled_run_is_ignored_when_push_run_succeeded(self):
        # Reproduces the live scheduled-skip shape: a later `skipped` run must
        # not win the latest-per-context selection over an earlier `success`.
        client = _checks_ready_client()
        client.branch_rules = _rules_payload(["backend-tests"])
        client.check_runs = {
            "backend-tests": [
                completed_check("backend-tests", COMMIT, conclusion="skipped", completed_at="2026-10-07T15:18:33Z"),
                completed_check("backend-tests", COMMIT, conclusion="success", completed_at="2026-10-07T03:01:52Z"),
            ]
        }
        conclusions = validate.validate_required_checks(client, COMMIT)
        self.assertEqual([c.name for c in conclusions], ["backend-tests"])

    def test_later_failure_after_success_still_fails(self):
        client = _checks_ready_client()
        client.branch_rules = _rules_payload(["backend-tests"])
        client.check_runs = {
            "backend-tests": [
                completed_check("backend-tests", COMMIT, conclusion="success", completed_at="2026-10-07T03:00:00Z"),
                completed_check("backend-tests", COMMIT, conclusion="failure", completed_at="2026-10-07T15:00:00Z"),
            ]
        }
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_only_skipped_runs_fail_closed(self):
        client = _checks_ready_client()
        client.branch_rules = _rules_payload(["backend-tests"])
        client.check_runs = {
            "backend-tests": [
                completed_check("backend-tests", COMMIT, conclusion="skipped", completed_at="2026-10-07T15:18:33Z"),
            ]
        }
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_missing_head_sha_fails_closed(self):
        client = _checks_ready_client()
        client.branch_rules = _rules_payload(["backend-tests"])
        run = completed_check("backend-tests", COMMIT)
        del run["head_sha"]
        client.check_runs = {"backend-tests": [run]}
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_missing_app_id_fails_closed(self):
        client = _checks_ready_client()
        client.branch_rules = _rules_payload(["backend-tests"])
        run = completed_check("backend-tests", COMMIT)
        run["app"] = {}
        client.check_runs = {"backend-tests": [run]}
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)

    def test_empty_ruleset_fails_closed(self):
        client = _checks_ready_client()
        client.branch_rules = _rules_payload([])
        with self.assertRaises(validate.ValidationError):
            validate.validate_required_checks(client, COMMIT)


# ---------------------------------------------------------------------------
# Normalized SBOM: UTC Z timestamp and unique-per-release namespace.
# ---------------------------------------------------------------------------
_RAW_SBOM = json.dumps(
    {
        "spdxVersion": "SPDX-2.3",
        "creationInfo": {"created": "2026-10-07T19:15:19Z", "creators": ["Tool: syft-1.50.0"]},
        "documentNamespace": "https://anchore.com/syft/dir/backend-abc",
        "name": "backend",
        "packages": [{"SPDXID": "SPDXRef-Package-x", "name": "x"}],
    }
).encode()


class SbomNormalizationTests(unittest.TestCase):
    def test_created_is_utc_z_not_offset(self):
        out = sbom.normalize_sbom(_RAW_SBOM, commit_date="2026-10-06T19:57:43-07:00", source_name="game-agent-backend")
        created = json.loads(out)["creationInfo"]["created"]
        self.assertTrue(created.endswith("Z"), msg=f"created not UTC-Z: {created!r}")
        self.assertNotIn("+", created)
        self.assertNotRegex(created, r"-\d{2}:\d{2}$")
        # Must be a valid SPDX instant: YYYY-MM-DDThh:mm:ssZ
        self.assertRegex(created, r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

    def test_utc_conversion_preserves_instant(self):
        # 2026-10-06T19:57:43-07:00 == 2026-10-07T02:57:43Z
        out = sbom.normalize_sbom(_RAW_SBOM, commit_date="2026-10-06T19:57:43-07:00", source_name="game-agent-backend")
        self.assertEqual(json.loads(out)["creationInfo"]["created"], "2026-10-07T02:57:43Z")

    def test_namespace_unique_per_release(self):
        a = sbom.normalize_sbom(
            _RAW_SBOM,
            commit_date="2026-10-07T00:00:00Z",
            source_name="game-agent-backend",
            tag="v0.1.0",
            commit="a" * 40,
        )
        b = sbom.normalize_sbom(
            _RAW_SBOM,
            commit_date="2026-10-07T00:00:00Z",
            source_name="game-agent-backend",
            tag="v0.2.0",
            commit="b" * 40,
        )
        self.assertNotEqual(json.loads(a)["documentNamespace"], json.loads(b)["documentNamespace"])

    def test_namespace_deterministic_for_same_release(self):
        a = sbom.normalize_sbom(
            _RAW_SBOM,
            commit_date="2026-10-07T00:00:00Z",
            source_name="game-agent-backend",
            tag="v0.1.0",
            commit="a" * 40,
        )
        b = sbom.normalize_sbom(
            _RAW_SBOM,
            commit_date="2026-10-07T00:00:00Z",
            source_name="game-agent-backend",
            tag="v0.1.0",
            commit="a" * 40,
        )
        self.assertEqual(a, b)

    def test_zulu_commit_date_passes_through(self):
        out = sbom.normalize_sbom(_RAW_SBOM, commit_date="2026-10-07T02:57:43Z", source_name="game-agent-backend")
        self.assertEqual(json.loads(out)["creationInfo"]["created"], "2026-10-07T02:57:43Z")


# ---------------------------------------------------------------------------
# Publish rebuilds the manifest and requires byte equality.
# ---------------------------------------------------------------------------
class ManifestRebindTests(unittest.TestCase):
    def test_rebuild_mismatch_rejected(self):
        downloaded = b'{"schema_version": "gbaw.release-manifest.v1", "tag": "v0.1.0"}\n'
        rebuilt = b'{"schema_version": "gbaw.release-manifest.v1", "tag": "v0.1.0", "x": 1}\n'
        with self.assertRaises(Exception):
            publish.assert_manifest_byte_identical(downloaded_manifest=downloaded, rebuilt_manifest=rebuilt)

    def test_rebuild_match_passes(self):
        same = b'{"schema_version": "gbaw.release-manifest.v1", "tag": "v0.1.0"}\n'
        publish.assert_manifest_byte_identical(downloaded_manifest=same, rebuilt_manifest=same)


# ---------------------------------------------------------------------------
# Robust cleanup under interruption and ambiguous responses.
# ---------------------------------------------------------------------------
def _publish_inputs(tag="v0.1.0"):
    # Local modules
    from release import artifacts as artifacts_mod

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


class InterruptClient(RecordingClient):
    def update_release(self, release_id, *, draft=None, prerelease=None, make_latest=None):
        self.calls.append("update_release")
        raise KeyboardInterrupt


class AmbiguousPublishClient(RecordingClient):
    def update_release(self, release_id, *, draft=None, prerelease=None, make_latest=None):
        super().update_release(release_id, draft=draft, prerelease=prerelease, make_latest=make_latest)
        raise RuntimeError("github api update-release failed with status 502")


class AmbiguousRefClient(RecordingClient):
    def create_ref(self, *, ref, sha):
        super().create_ref(ref=ref, sha=sha)  # applied server-side
        raise RuntimeError("github api create-ref failed with status 502")


class CleanupRobustnessTests(unittest.TestCase):
    def test_interruption_after_ref_leaves_nothing_stable(self):
        client = InterruptClient()
        client.main_head = COMMIT
        with self.assertRaises(BaseException):
            publish.publish_release(client, _publish_inputs())
        # SIGINT during the final PATCH must still trigger cleanup: the tag ref
        # and the draft must be removed.
        self.assertNotIn("tags/v0.1.0", client.existing_refs)
        self.assertEqual([r for r in client.releases], [])

    def test_ambiguous_publish_does_not_delete_published_release(self):
        client = AmbiguousPublishClient()
        client.main_head = COMMIT
        with self.assertRaises(RuntimeError):
            publish.publish_release(client, _publish_inputs())
        # The PATCH was applied server-side: re-reading state shows a published
        # (non-draft) release, so cleanup must NOT delete it or its tag.
        self.assertTrue(any(not r.get("draft", True) for r in client.releases))
        self.assertIn("tags/v0.1.0", client.existing_refs)

    def test_ambiguous_ref_creation_cleans_up_the_ref_it_made(self):
        client = AmbiguousRefClient()
        client.main_head = COMMIT
        with self.assertRaises(RuntimeError):
            publish.publish_release(client, _publish_inputs())
        # create_ref was applied server-side but the client saw an error. Since
        # nothing was published, the orphaned ref and draft must be removed.
        self.assertNotIn("tags/v0.1.0", client.existing_refs)
        self.assertFalse(any(not r.get("draft", True) for r in client.releases))


class HttpTimeoutTests(unittest.TestCase):
    def test_default_transport_sets_a_finite_timeout(self):
        # The urllib transport must pass a finite timeout so a stalled call
        # cannot hold the run for the full job budget.
        self.assertIsNotNone(
            getattr(__import__("release.github_client", fromlist=["x"]), "_HTTP_TIMEOUT_SECONDS", None)
        )
        self.assertLess(__import__("release.github_client", fromlist=["x"])._HTTP_TIMEOUT_SECONDS, 120)


# ---------------------------------------------------------------------------
# Operations-mode proof fails closed on ANY non-doc reference.
# ---------------------------------------------------------------------------
def _ops_fake_repo(root: Path):
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md").write_text(
        "The default is `disabled`.\n", encoding="utf-8"
    )


class OperationsModeFailClosedTests(unittest.TestCase):
    def test_python_default_operate_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend" / "src" / "config").mkdir(parents=True)
            (root / "backend" / "src" / "config" / "settings.py").write_text(
                'import os\nOPERATIONS_MODE = os.environ.get("GBAW_OPERATIONS_MODE", "operate")\n',
                encoding="utf-8",
            )
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_cfn_parameter_default_remediate_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            cfn = root / "infrastructure" / "cloudformation"
            cfn.mkdir(parents=True)
            (cfn / "99-ops.yaml").write_text(
                "Parameters:\n  OperationsMode:\n    Type: String\n    Default: remediate\n"
                "Resources:\n  Fn:\n    Properties:\n      Environment:\n        Variables:\n"
                "          GBAW_OPERATIONS_MODE: !Ref OperationsMode\n",
                encoding="utf-8",
            )
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_shell_variable_default_operate_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "deploy.sh").write_text(
                'OPS_MODE="${OPS_MODE:-operate}"\n' 'agentcore launch --env GBAW_OPERATIONS_MODE="$OPS_MODE"\n',
                encoding="utf-8",
            )
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_env_example_reference_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "ui").mkdir(parents=True)
            (root / "ui" / ".env.local.example").write_text("GBAW_OPERATIONS_MODE=operate\n", encoding="utf-8")
            with self.assertRaises(manifest.OperationsModeProofError):
                manifest.prove_operations_mode_disabled(root)

    def test_doc_and_release_tooling_references_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            # Docs may discuss the variable freely.
            (root / "docs").mkdir(parents=True, exist_ok=True)
            (root / "docs" / "THREAT_MODEL.md").write_text("GBAW_OPERATIONS_MODE operate advise\n", encoding="utf-8")
            # The release tooling itself references it (this proof's own regex).
            (root / "scripts" / "release").mkdir(parents=True)
            (root / "scripts" / "release" / "manifest.py").write_text("GBAW_OPERATIONS_MODE\n", encoding="utf-8")
            # Tests may reference it.
            (root / "scripts" / "test").mkdir(parents=True)
            (root / "scripts" / "test" / "test_release_x.py").write_text(
                "GBAW_OPERATIONS_MODE=operate\n", encoding="utf-8"
            )
            self.assertEqual(manifest.prove_operations_mode_disabled(root), "disabled")


# ---------------------------------------------------------------------------
# Evidence binding: duplicate keys, permalink issue, author permission.
# ---------------------------------------------------------------------------
class EvidenceDuplicateKeyTests(unittest.TestCase):
    def test_duplicate_json_keys_rejected(self):
        # Local modules
        from release import evidence

        gates = {name: "pass" for name in evidence.REQUIRED_GATES}
        body = (
            "```json\n"
            "{\n"
            f'  "schema_version": "{evidence.EVIDENCE_SCHEMA_VERSION}",\n'
            f'  "commit": "{COMMIT}",\n'
            f'  "commit": "{"b" * 40}",\n'
            f'  "gates": {json.dumps(gates)}\n'
            "}\n"
            "```"
        )
        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(body, expected_commit=COMMIT)


class EvidencePermalinkIssueTests(unittest.TestCase):
    def _ready(self):
        # Local modules
        from release import evidence

        client = RecordingClient()
        gates = {name: "pass" for name in evidence.REQUIRED_GATES}
        doc = {"schema_version": evidence.EVIDENCE_SCHEMA_VERSION, "commit": COMMIT, "gates": gates}
        client.comments[55] = {
            "author_association": "OWNER",
            "issue_url": f"https://api.github.com/repos/{client.owner}/{client.repo}/issues/537",
            "body": f"```json\n{json.dumps(doc, indent=2)}\n```",
        }
        client.collaborator_permissions = {"alice": "admin"}
        client.comments[55]["user"] = {"login": "alice"}
        return client

    def test_issue_number_mismatch_rejected(self):
        client = self._ready()
        with self.assertRaises(validate.ValidationError):
            validate.validate_evidence_comment(
                client, comment_id=55, commit=COMMIT, scan_fn=lambda _b: [], expected_issue=999
            )

    def test_issue_number_match_passes(self):
        client = self._ready()
        digest, gates, waivers = validate.validate_evidence_comment(
            client, comment_id=55, commit=COMMIT, scan_fn=lambda _b: [], expected_issue=537
        )
        self.assertEqual(len(digest), 64)

    def test_author_without_write_permission_rejected(self):
        client = self._ready()
        client.collaborator_permissions = {"alice": "read"}
        with self.assertRaises(validate.ValidationError):
            validate.validate_evidence_comment(
                client, comment_id=55, commit=COMMIT, scan_fn=lambda _b: [], expected_issue=537
            )


# ---------------------------------------------------------------------------
# Waivers allowed only for p0_p1_closed_or_waived.
# ---------------------------------------------------------------------------
class WaiverRestrictionTests(unittest.TestCase):
    def _doc(self, gate, status, waiver_ref="#412"):
        # Local modules
        from release import evidence

        gates = {name: "pass" for name in evidence.REQUIRED_GATES}
        gates[gate] = status
        body = {
            "schema_version": evidence.EVIDENCE_SCHEMA_VERSION,
            "commit": COMMIT,
            "gates": gates,
            "waivers": {gate: waiver_ref} if status == "waived" else {},
        }
        return f"```json\n{json.dumps(body, indent=2)}\n```"

    def test_waiving_p0_p1_allowed(self):
        # Local modules
        from release import evidence

        doc = evidence.parse_and_validate(self._doc("p0_p1_closed_or_waived", "waived"), expected_commit=COMMIT)
        self.assertEqual(doc.gates["p0_p1_closed_or_waived"], "waived")

    def test_waiving_other_gate_rejected(self):
        # Local modules
        from release import evidence

        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(self._doc("unauthorized_requests_denied", "waived"), expected_commit=COMMIT)

    def test_waiving_public_content_scan_rejected(self):
        # Local modules
        from release import evidence

        with self.assertRaises(evidence.EvidenceError):
            evidence.parse_and_validate(self._doc("public_content_scan_passes", "waived"), expected_commit=COMMIT)


# ---------------------------------------------------------------------------
# Ops-mode manifest wording -- the proof's manifest field names what it checked.
# ---------------------------------------------------------------------------
class ManifestOpsModeWordingTests(unittest.TestCase):
    def test_manifest_records_what_was_proven(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ops_fake_repo(root)
            (root / "backend" / "src" / "agents").mkdir(parents=True)
            (root / "backend" / "src" / "agents" / "optimized_prompts.py").write_text("P='x'\n", encoding="utf-8")
            (root / "backend" / "uv.lock").write_text("l\n", encoding="utf-8")
            (root / "backend" / "requirements.txt").write_text("r\n", encoding="utf-8")
            (root / "ui").mkdir()
            (root / "ui" / "package-lock.json").write_text("{}\n", encoding="utf-8")
            m = manifest.build_manifest(
                root,
                tag="v0.1.0",
                release_name="v0.1.0",
                commit=COMMIT,
                commit_date="2024-01-02T03:04:05Z",
                required_checks=[manifest.CheckRunResult(name="backend-tests", conclusion="success")],
                evidence_digest="d" * 64,
                gate_summary={"a": "pass"},
                attached_file_names=["SHA256SUMS", "release-manifest.json"],
            )
            # The manifest must carry an explicit record of the fail-closed
            # source scan, not just the string "disabled".
            self.assertIn("default_operations_mode", m)
            self.assertEqual(m["default_operations_mode"], "disabled")


if __name__ == "__main__":
    unittest.main()
