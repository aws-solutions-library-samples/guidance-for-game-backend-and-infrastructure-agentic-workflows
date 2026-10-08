"""Command-line entrypoints invoked by ``.github/workflows/release.yml``.

Every dispatch input reaches these commands through the environment, never
through shell interpolation. The commands are:

* ``validate``      -- run all pre-write checks; write a validation summary.
* ``build-artifacts`` -- generate the deterministic artifact set TWICE from two
  independent syft runs into separate temp directories, fail unless both builds
  are byte-identical, then write the verified set (manifest + normalized SBOMs +
  SHA256SUMS), guarded. Read-only token.
* ``verify-environment`` -- fail closed unless the ``release`` environment exists
  and has a required-reviewers protection rule. Read-only (``actions: read``).
* ``publish``       -- (non-dry-run only) re-run every pre-write validation, then
  publish exactly the downloaded, verified artifact set. Never rebuilds
  artifacts.

Inputs (env):
    GBAW_RELEASE_COMMIT          full 40-hex commit sha
    GBAW_RELEASE_TAG             vMAJOR.MINOR.PATCH[-rc.N]
    GBAW_RELEASE_EVIDENCE_URL    issue-comment permalink in this repository
    GBAW_RELEASE_REPO            "owner/repo"
    GITHUB_TOKEN                 default workflow token
    GBAW_RELEASE_OUTPUT_DIR      temp dir the verified artifact set is written to
    GBAW_RELEASE_INPUT_DIR       dir the publish job downloaded the artifact set to
    GBAW_RELEASE_BACKEND_SBOM    path to raw backend SPDX json (syft run 1)
    GBAW_RELEASE_FRONTEND_SBOM   path to raw frontend SPDX json (syft run 1)
    GBAW_RELEASE_BACKEND_SBOM_2  path to raw backend SPDX json (syft run 2)
    GBAW_RELEASE_FRONTEND_SBOM_2 path to raw frontend SPDX json (syft run 2)
    GBAW_RELEASE_COMMIT_DATE     ISO-8601 commit date (resolved by the workflow)

The module scans external text (the evidence comment, release notes) with the
repository ``check_public_content.py`` scanner so the release path uses the
same rules as CI.
"""

from __future__ import annotations

# Standard library
import argparse
import os
import subprocess
import sys
from pathlib import Path

# Resolve ``scripts/`` onto the path so sibling modules import cleanly whether
# invoked as ``python -m release.cli`` from scripts/ or as a file.
_SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

# Local modules
from release import artifacts as artifacts_mod  # noqa: E402
from release import manifest as manifest_mod  # noqa: E402
from release import publish as publish_mod  # noqa: E402
from release import sbom as sbom_mod  # noqa: E402
from release import validate as validate_mod  # noqa: E402
from release.artifact_guard import BACKEND_SBOM_NAME, CHECKSUMS_NAME, FRONTEND_SBOM_NAME, MANIFEST_NAME  # noqa: E402
from release.comment_url import parse_comment_url  # noqa: E402
from release.github_client import GitHubClient  # noqa: E402

REPO_ROOT = _SCRIPTS_ROOT.parent


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise SystemExit("GBAW_RELEASE_REPO must be 'owner/repo'")
    return owner, name


def _public_content_scanner(repo_root: Path):
    script = repo_root / "scripts" / "check_public_content.py"

    def scan_text(text: str) -> list[str]:
        # Standard library
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(text)
            temp_path = handle.name
        try:
            result = subprocess.run(
                [sys.executable, str(script), temp_path],
                capture_output=True,
                text=True,
            )
            return [] if result.returncode == 0 else result.stdout.splitlines() or ["public-content"]
        finally:
            os.unlink(temp_path)

    def scan_files(paths: list[str]) -> list[str]:
        result = subprocess.run(
            [sys.executable, str(script), *paths],
            capture_output=True,
            text=True,
        )
        return [] if result.returncode == 0 else result.stdout.splitlines() or ["public-content"]

    return scan_text, scan_files


def _client() -> tuple[GitHubClient, str, str]:
    owner, repo = _split_repo(_env("GBAW_RELEASE_REPO"))
    client = GitHubClient(owner=owner, repo=repo, token=_env("GITHUB_TOKEN"))
    return client, owner, repo


def _run_validation(client: GitHubClient, owner: str, repo: str) -> validate_mod.ValidationResult:
    commit = _env("GBAW_RELEASE_COMMIT")
    tag = _env("GBAW_RELEASE_TAG")
    url = _env("GBAW_RELEASE_EVIDENCE_URL")
    comment_ref = parse_comment_url(url, expected_owner=owner, expected_repo=repo)
    scan_text, scan_files = _public_content_scanner(REPO_ROOT)
    return validate_mod.run_full_validation(
        client,
        REPO_ROOT,
        commit=commit,
        tag=tag,
        evidence_comment_id=comment_ref.comment_id,
        evidence_issue=comment_ref.issue,
        release_name=tag,
        comment_scan_fn=scan_text,
        file_scan_fn=scan_files,
    )


def cmd_validate(_args: argparse.Namespace) -> int:
    client, owner, repo = _client()
    result = _run_validation(client, owner, repo)
    print(f"validation ok: tag={result.tag} commit={result.commit[:12]} checks={len(result.check_conclusions)}")
    waived = [name for name, status in result.gate_summary.items() if status == "waived"]
    print(f"gates: {len(result.gate_summary)} total, {len(waived)} waived")
    return 0


def _build_artifact_set(
    *, backend_sbom_path: str | None = None, frontend_sbom_path: str | None = None
) -> tuple[list, bytes, validate_mod.ValidationResult]:
    client, owner, repo = _client()
    result = _run_validation(client, owner, repo)

    commit_date = _env("GBAW_RELEASE_COMMIT_DATE")
    backend_raw = Path(backend_sbom_path or _env("GBAW_RELEASE_BACKEND_SBOM")).read_bytes()
    frontend_raw = Path(frontend_sbom_path or _env("GBAW_RELEASE_FRONTEND_SBOM")).read_bytes()
    backend_sbom = sbom_mod.normalize_sbom(
        backend_raw,
        commit_date=commit_date,
        source_name="game-agent-backend",
        tag=result.tag,
        commit=result.commit,
    )
    frontend_sbom = sbom_mod.normalize_sbom(
        frontend_raw,
        commit_date=commit_date,
        source_name="game-agent-frontend",
        tag=result.tag,
        commit=result.commit,
    )

    checks = [manifest_mod.CheckRunResult(name=c.name, conclusion=c.conclusion) for c in result.check_conclusions]
    manifest = manifest_mod.build_manifest(
        REPO_ROOT,
        tag=result.tag,
        release_name=result.release_name,
        commit=result.commit,
        commit_date=commit_date,
        required_checks=checks,
        evidence_digest=result.evidence_digest,
        gate_summary=result.gate_summary,
        waivers=result.waivers,
        attached_file_names=[MANIFEST_NAME, BACKEND_SBOM_NAME, FRONTEND_SBOM_NAME, CHECKSUMS_NAME],
    )
    manifest_bytes = manifest_mod.serialize_manifest(manifest)
    artifacts, _sums = artifacts_mod.assemble_artifacts(
        manifest_bytes=manifest_bytes,
        backend_sbom_bytes=backend_sbom,
        frontend_sbom_bytes=frontend_sbom,
    )
    return artifacts, manifest_bytes, result


def cmd_build_artifacts(_args: argparse.Namespace) -> int:
    """Build the artifact set TWICE from two independent syft runs and prove
    the normalized output is byte-identical before writing the verified set.

    The two SBOM input pairs come from two independent syft invocations the
    workflow runs into separate directories; this command normalizes and
    assembles each, requires every artifact's bytes to match across the two
    builds, then writes exactly the verified bytes to the output directory.
    This job holds only a read-only token.
    """

    out_dir = Path(_env("GBAW_RELEASE_OUTPUT_DIR"))
    out_dir.mkdir(parents=True, exist_ok=True)

    first, _m1, _r1 = _build_artifact_set(
        backend_sbom_path=_env("GBAW_RELEASE_BACKEND_SBOM"),
        frontend_sbom_path=_env("GBAW_RELEASE_FRONTEND_SBOM"),
    )
    second, _m2, _r2 = _build_artifact_set(
        backend_sbom_path=_env("GBAW_RELEASE_BACKEND_SBOM_2"),
        frontend_sbom_path=_env("GBAW_RELEASE_FRONTEND_SBOM_2"),
    )

    first_by_name = {a.name: a.data for a in first}
    second_by_name = {a.name: a.data for a in second}
    if set(first_by_name) != set(second_by_name):
        raise SystemExit("non-deterministic build: artifact name sets differ between the two builds")
    for name in sorted(first_by_name):
        if first_by_name[name] != second_by_name[name]:
            raise SystemExit(f"non-deterministic build: {name} differs between the two independent builds")

    for artifact in first:
        (out_dir / artifact.name).write_bytes(artifact.data)
        print(f"wrote {artifact.name} ({len(artifact.data)} bytes, verified byte-identical across two builds)")
    return 0


def cmd_verify_environment(_args: argparse.Namespace) -> int:
    """Fail closed unless the ``release`` deployment environment is protected.

    Runs before any write in the publish job. Needs only ``actions: read``.
    """

    client, _owner, _repo = _client()
    validate_mod.validate_environment_protected(client)
    print(f"release environment '{validate_mod.RELEASE_ENVIRONMENT}' is protected by required reviewers")
    return 0


def cmd_publish(_args: argparse.Namespace) -> int:
    if os.environ.get("GBAW_RELEASE_DRY_RUN", "true").lower() != "false":
        raise SystemExit("publish refused: this is a dry run")

    client, owner, repo = _client()

    # Re-run every pre-write validation against the live API. This binds the
    # tag, commit, required checks, and evidence immediately before publishing;
    # it does NOT rebuild any artifact.
    result = _run_validation(client, owner, repo)

    # Consume the downloaded, verified artifact set produced by build-artifacts.
    # Publication never regenerates artifacts.
    input_dir = Path(_env("GBAW_RELEASE_INPUT_DIR"))
    notes = (REPO_ROOT / "docs" / "releases" / f"{result.tag}.md").read_text(encoding="utf-8")

    # Rebuild the manifest from THIS checkout and the publish-time validation
    # result; the downloaded manifest must be byte-identical to it, so an
    # evidence edit during the approval wait cannot be published unnoticed.
    commit_date = _env("GBAW_RELEASE_COMMIT_DATE")
    checks = [manifest_mod.CheckRunResult(name=c.name, conclusion=c.conclusion) for c in result.check_conclusions]
    rebuilt_manifest = manifest_mod.serialize_manifest(
        manifest_mod.build_manifest(
            REPO_ROOT,
            tag=result.tag,
            release_name=result.release_name,
            commit=result.commit,
            commit_date=commit_date,
            required_checks=checks,
            evidence_digest=result.evidence_digest,
            gate_summary=result.gate_summary,
            waivers=result.waivers,
            attached_file_names=[MANIFEST_NAME, BACKEND_SBOM_NAME, FRONTEND_SBOM_NAME, CHECKSUMS_NAME],
        )
    )

    metadata = publish_mod.PublishMetadata(
        tag=result.tag,
        commit=result.commit,
        release_name=result.release_name,
        notes=notes,
        tagger_name=os.environ.get("GBAW_RELEASE_TAGGER_NAME", "github-actions[bot]"),
        tagger_email=os.environ.get(
            "GBAW_RELEASE_TAGGER_EMAIL",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ),
        commit_date=commit_date,
    )
    published = publish_mod.publish_release_from_dir(client, input_dir, metadata, rebuilt_manifest=rebuilt_manifest)
    print(f"published release id={published.release_id} tag_ref_created={published.tag_ref_created}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate").set_defaults(func=cmd_validate)
    sub.add_parser("build-artifacts").set_defaults(func=cmd_build_artifacts)
    sub.add_parser("verify-environment").set_defaults(func=cmd_verify_environment)
    sub.add_parser("publish").set_defaults(func=cmd_publish)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
