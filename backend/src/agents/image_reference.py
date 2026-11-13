"""Code-owned projection of a container image reference (#529).

Agones pod templates and GameLift container definitions both name a container
image. The raw reference can embed an account identifier and a registry
endpoint (for example a private Amazon ECR host), so it never crosses into
model context verbatim. This module splits a reference into reviewed parts and
keeps only the parts a migration assessment needs:

* ``registryType`` — a code-owned classification of the registry host
  (``ecr-private``, ``ecr-public``, ``docker-hub``, ``other``). The host itself
  is always dropped.
* ``registryRegion`` — the AWS Region of a private ECR registry, when the host
  carries one that matches the Region grammar.
* ``repository``, ``tag``, ``digest`` — each validated against a strict
  grammar and dropped when it fails or carries an account-like or network
  shape.

The projection is deterministic and has no I/O.
"""

from __future__ import annotations

# Standard library
import re
from typing import Any

MAX_IMAGE_REFERENCE_LENGTH = 512
MAX_REPOSITORY_LENGTH = 128
MAX_TAG_LENGTH = 128

REGISTRY_ECR_PRIVATE = "ecr-private"
REGISTRY_ECR_PUBLIC = "ecr-public"
REGISTRY_DOCKER_HUB = "docker-hub"
REGISTRY_OTHER = "other"

_CONTROL_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f]")
# Grammars are applied with ``fullmatch`` so a trailing newline is rejected.
_ECR_PRIVATE_HOST = re.compile(r"^\d{12}\.dkr\.ecr(?:-fips)?\.([a-z]{2,}(?:-[a-z0-9]+){1,4})\.amazonaws\.com(?:\.cn)?$")
_DOCKER_HUB_HOSTS = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
_REGION = re.compile(r"^[a-z]{2,}(-[a-z0-9]+){1,4}$")
_REPOSITORY = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,7}$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
# Account-like and network-coordinate shapes are refused in every kept part.
_SENSITIVE = re.compile(r"\d{12}|\d+(?:\.\d+){3}|://|arn:", re.IGNORECASE)


def _registry_split(reference: str) -> tuple[str | None, str]:
    """Split ``reference`` into ``(registry_host, remainder)``.

    Docker's rule: the first path component is a registry host when it contains
    a dot or a colon, or is ``localhost``.
    """
    first, sep, rest = reference.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first.lower(), rest
    return None, reference


def _classify_registry(host: str | None) -> tuple[str, str | None]:
    if host is None or host in _DOCKER_HUB_HOSTS:
        return REGISTRY_DOCKER_HUB, None
    if host == "public.ecr.aws":
        return REGISTRY_ECR_PUBLIC, None
    match = _ECR_PRIVATE_HOST.fullmatch(host)
    if match:
        region = match.group(1)
        return REGISTRY_ECR_PRIVATE, region if _REGION.fullmatch(region) else None
    return REGISTRY_OTHER, None


def project_image_reference(value: Any) -> dict[str, Any] | None:
    """Return the reviewed parts of an image reference, or ``None``.

    ``None`` means the value is not a usable reference (not a string, empty,
    oversized, or carries control characters). A usable reference always
    yields ``registryType``; each other part appears only when it validates.
    ``complete`` is false when a present part was dropped, so a consumer can
    tell a partial reference from a whole one.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_IMAGE_REFERENCE_LENGTH:
        return None
    if _CONTROL_CHARS.search(value):
        return None

    reference, at, digest = value.partition("@")
    host, remainder = _registry_split(reference)
    registry_type, registry_region = _classify_registry(host)

    repository, tag = remainder, None
    slash = remainder.rfind("/")
    colon = remainder.rfind(":")
    if colon > slash:
        repository, tag = remainder[:colon], remainder[colon + 1 :]
    if registry_type == REGISTRY_DOCKER_HUB and "/" not in repository and repository:
        repository = f"library/{repository}"

    out: dict[str, Any] = {"registryType": registry_type}
    complete = True
    if registry_region:
        out["registryRegion"] = registry_region
    if (
        repository
        and len(repository) <= MAX_REPOSITORY_LENGTH
        and _REPOSITORY.fullmatch(repository)
        and not _SENSITIVE.search(repository)
    ):
        out["repository"] = repository
    else:
        complete = False
    if tag is not None:
        if len(tag) <= MAX_TAG_LENGTH and _TAG.fullmatch(tag) and not _SENSITIVE.search(tag):
            out["tag"] = tag
        else:
            complete = False
    if at:
        if _DIGEST.fullmatch(digest):
            out["digest"] = digest
        else:
            complete = False
    out["complete"] = complete
    return out


__all__ = [
    "MAX_IMAGE_REFERENCE_LENGTH",
    "REGISTRY_DOCKER_HUB",
    "REGISTRY_ECR_PRIVATE",
    "REGISTRY_ECR_PUBLIC",
    "REGISTRY_OTHER",
    "project_image_reference",
]
