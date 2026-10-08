"""Public-safety guard for release artifacts.

Everything a release attaches is world-readable forever. This guard enforces
two independent rules before any artifact is uploaded:

1. **Exact allowlist of filenames.** Only ``release-manifest.json``, the two
   SBOM filenames, and ``SHA256SUMS`` may be attached. Anything else -- most
   dangerously a source archive, which GitHub already supplies automatically
   for the tag -- is rejected.

2. **Forbidden content.** No ARNs, account-ID patterns, credentials/tokens,
   ``.env`` / ``.bedrock_agentcore`` material, private registry or internal
   hostnames, or source-archive signatures may appear in any attached bytes.

The guard is intentionally conservative: it fails closed. It reuses the
repository public-content rules where possible but adds release-specific
denials (ARNs, private registries, archive magic bytes) that the tracked-file
scanner does not need.
"""

from __future__ import annotations

# Standard library
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Reuse the repository public-content scanner's compiled account-id pattern so
# the guard and CI agree on what a 12-digit account id is. The scanner uses
# ALPHANUMERIC borders, so a 12-decimal-digit run bordered by hex letters --
# which is common inside syft SPDX ids and SHA-256 digests -- is NOT treated as
# an account id (a digit-only border would reject every real SBOM).
_SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))
# Local modules
from check_public_content import ACCOUNT_ID_PATTERN as _ACCOUNT_ID_RE  # noqa: E402
from check_public_content import SYNTHETIC_ACCOUNT_IDS as _SYNTHETIC_ACCOUNT_IDS  # noqa: E402

# The four, and only four, attachable artifact names.
MANIFEST_NAME = "release-manifest.json"
BACKEND_SBOM_NAME = "backend-sbom.spdx.json"
FRONTEND_SBOM_NAME = "frontend-sbom.spdx.json"
CHECKSUMS_NAME = "SHA256SUMS"

ALLOWED_ARTIFACT_NAMES = frozenset({MANIFEST_NAME, BACKEND_SBOM_NAME, FRONTEND_SBOM_NAME, CHECKSUMS_NAME})

# Public package registries that are allowed to appear (SBOMs reference them).
ALLOWED_REGISTRY_HOSTS = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "www.npmjs.com",
        "npmjs.com",
        "github.com",
        "api.github.com",
        "spdx.org",
        "creativecommons.org",
        "cyclonedx.org",
        "anchore.com",
    }
)

_ARN_RE = re.compile(r"arn:aws[a-z0-9-]*:")
_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_SYNTHETIC_ACCESS_KEYS = frozenset({"AKIAIOSFODNN7EXAMPLE"})
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,255}|github_pat_[A-Za-z0-9_]{20,255})\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_INTERNAL_DOMAIN_RE = re.compile(
    r"\b(?:"
    + "|".join(
        [
            r"a2z\.com",
            # Build the amazon.com-family markers from fragments so no literal
            # internal domain appears in this public source file.
            r"(?:code|issues|sim|w)\." + "amazon" + r"\." + "com",
            "quip-" + "amazon" + r"\." + "com",
            r"aws\." + "dev",
            r"[A-Za-z0-9.-]+\.corp\." + "amazon" + r"\." + "com",
            r"[0-9]+\.dkr\.ecr\.[a-z0-9-]+\." + "amazonaws" + r"\." + "com",
            r"[a-z0-9-]+\.execute-api\.[a-z0-9-]+\." + "amazonaws" + r"\." + "com",
        ]
    )
    + r")\b",
    re.IGNORECASE,
)
# .env / agentcore secret material markers.
_ENV_MATERIAL_RE = re.compile(
    r"(?:^|\n)\s*(?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|GBAW_[A-Z0-9_]*(?:ARN|SECRET|TOKEN))\s*="
)
_BEDROCK_AGENTCORE_RE = re.compile(r"\.bedrock_agentcore(?:\.yaml)?\b")

# Host suffixes that are never public package registries. Built from fragments
# so no literal internal/cloud domain appears in this public source file.
_NON_PUBLIC_HOST_SUFFIXES = (
    "." + "amazonaws" + "." + "com",
    "." + "amazon" + "." + "com",
    "." + "aws" + "." + "dev",
)

# Archive magic bytes: zip (PK\x03\x04), gzip (\x1f\x8b), tar ("ustar" at 257).
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"


class ArtifactRejected(ValueError):
    """Raised when an artifact name or content violates the public-safety rules."""


@dataclass(frozen=True)
class Artifact:
    name: str
    data: bytes
    content_type: str = "application/octet-stream"


@dataclass
class GuardResult:
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _looks_like_archive(name: str, data: bytes) -> bool:
    lowered = name.lower()
    if lowered.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".gz")):
        return True
    if data[:4] == _ZIP_MAGIC or data[:2] == _GZIP_MAGIC:
        return True
    if len(data) > 262 and data[257:262] == b"ustar":
        return True
    return False


def _hostnames(text: str) -> set[str]:
    hosts: set[str] = set()
    for match in re.finditer(r"https?://([A-Za-z0-9.\-]+)", text):
        hosts.add(match.group(1).lower())
    return hosts


def scan_content(name: str, data: bytes) -> list[str]:
    """Return a list of violation codes for a single artifact's bytes."""

    violations: list[str] = []

    if _looks_like_archive(name, data):
        violations.append("source-or-binary-archive")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Non-UTF-8 attachable artifact is itself suspicious for this release.
        violations.append("non-utf8-content")
        return sorted(set(violations))

    if _ARN_RE.search(text):
        violations.append("arn")
    for match in _ACCOUNT_ID_RE.finditer(text):
        if match.group(0) not in _SYNTHETIC_ACCOUNT_IDS:
            violations.append("account-id")
            break
    for match in _ACCESS_KEY_RE.finditer(text):
        if match.group(0) not in _SYNTHETIC_ACCESS_KEYS:
            violations.append("access-key")
            break
    if _GITHUB_TOKEN_RE.search(text):
        violations.append("github-token")
    if _PRIVATE_KEY_RE.search(text):
        violations.append("private-key")
    if _JWT_RE.search(text):
        violations.append("jwt")
    if _INTERNAL_DOMAIN_RE.search(text):
        violations.append("internal-or-private-host")
    if _ENV_MATERIAL_RE.search(text):
        violations.append("env-material")
    if _BEDROCK_AGENTCORE_RE.search(text):
        violations.append("bedrock-agentcore-material")

    for host in _hostnames(text):
        bare = host[4:] if host.startswith("www.") else host
        if host in ALLOWED_REGISTRY_HOSTS or bare in ALLOWED_REGISTRY_HOSTS:
            continue
        if any(host.endswith(suffix) for suffix in _NON_PUBLIC_HOST_SUFFIXES):
            violations.append("non-public-registry-host")
            break

    return sorted(set(violations))


def guard_artifacts(artifacts: list[Artifact]) -> GuardResult:
    """Validate the full set: exact allowlist of names plus content scan.

    Fails closed. The result lists every violation found so a caller can log a
    bounded, de-duplicated summary.
    """

    result = GuardResult()
    names = [a.name for a in artifacts]

    for name in names:
        if name not in ALLOWED_ARTIFACT_NAMES:
            result.violations.append(f"disallowed-name:{name}")

    name_set = set(names)
    if len(name_set) != len(names):
        result.violations.append("duplicate-artifact-name")
    missing = ALLOWED_ARTIFACT_NAMES - name_set
    if missing:
        result.violations.append(f"missing-required-artifact:{sorted(missing)}")

    for artifact in artifacts:
        for violation in scan_content(artifact.name, artifact.data):
            result.violations.append(f"{artifact.name}:{violation}")

    return result


def assert_artifacts_safe(artifacts: list[Artifact]) -> None:
    result = guard_artifacts(artifacts)
    if not result.ok:
        raise ArtifactRejected("; ".join(sorted(set(result.violations))))
