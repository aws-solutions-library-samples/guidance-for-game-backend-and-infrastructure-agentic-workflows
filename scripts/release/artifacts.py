"""Assemble the complete, deterministic release artifact set.

Given the manifest bytes and the two normalized SBOM bytes, this builds the
``SHA256SUMS`` file and returns the full guarded artifact list. The result is
validated by the exclusion guard before it is returned, so a caller can never
accidentally proceed with an unsafe or off-allowlist set.
"""

from __future__ import annotations

from . import checksums
from .artifact_guard import (
    BACKEND_SBOM_NAME,
    CHECKSUMS_NAME,
    FRONTEND_SBOM_NAME,
    MANIFEST_NAME,
    Artifact,
    assert_artifacts_safe,
)


def assemble_artifacts(
    *,
    manifest_bytes: bytes,
    backend_sbom_bytes: bytes,
    frontend_sbom_bytes: bytes,
) -> tuple[list[Artifact], bytes]:
    """Return (artifacts, sha256sums_bytes).

    ``SHA256SUMS`` covers the three other artifacts and is itself appended to
    the returned artifact list. The complete set is guarded before return.
    """

    covered = {
        MANIFEST_NAME: manifest_bytes,
        BACKEND_SBOM_NAME: backend_sbom_bytes,
        FRONTEND_SBOM_NAME: frontend_sbom_bytes,
    }
    sums = checksums.build_sha256sums(covered)

    artifacts = [
        Artifact(name=MANIFEST_NAME, data=manifest_bytes, content_type="application/json"),
        Artifact(name=BACKEND_SBOM_NAME, data=backend_sbom_bytes, content_type="application/json"),
        Artifact(name=FRONTEND_SBOM_NAME, data=frontend_sbom_bytes, content_type="application/json"),
        Artifact(name=CHECKSUMS_NAME, data=sums, content_type="text/plain"),
    ]
    assert_artifacts_safe(artifacts)
    return artifacts, sums
