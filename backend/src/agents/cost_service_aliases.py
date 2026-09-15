"""Deterministic AWS service alias and family resolution for cost snapshots.

This module resolves common AWS service names (for example ``EC2`` or
``Amazon S3``) to the canonical Cost Explorer ``SERVICE`` dimension values that
are present in an already-cached, immutable report snapshot. It performs no
network access, never issues a Cost Explorer query, and never fuzzy-matches.
Resolution is a pure function of the requested names and the canonical service
names available in the snapshot, so callers keep their existing fail-closed and
reconciliation guarantees.

Resolution rules:

* Inputs are trimmed of surrounding whitespace.
* An exact canonical match (case-sensitive, then case-insensitive) takes
  precedence over any alias.
* Case-insensitive matching is only used when a casefolded name is unambiguous
  in the snapshot. If two distinct canonical names collide under casefold, the
  case-insensitive lookup fails closed for that name rather than letting
  snapshot iteration order silently pick a winner; a case-sensitive exact match
  for either colliding name still resolves.
* A recognized alias expands to every canonical family member that is present
  in the snapshot, in a fixed deterministic order.
* Only targets present in the snapshot are emitted; absent family members are
  silently skipped.
* Results preserve request order, expand families in their fixed order, and are
  deduplicated across mixed alias and exact requests.
* A requested name that matches neither an exact canonical name nor an alias
  with at least one present target is reported as missing so the caller can
  fail closed.
"""

from __future__ import annotations

# Standard library
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Fixed alias families. Each alias key maps to an ordered tuple of canonical
# Cost Explorer SERVICE dimension values. The order is intentional and stable:
# only members present in a snapshot are emitted, but present members always
# appear in this fixed order regardless of how the snapshot enumerates them.
_EC2_FAMILY: tuple[str, ...] = (
    "EC2 - Other",
    "Amazon Elastic Compute Cloud - Compute",
)
_S3_FAMILY: tuple[str, ...] = ("Amazon Simple Storage Service",)

# Alias keys are matched case-insensitively (via casefold) after trimming. Both
# the short form and the "Amazon"-prefixed common form map to the same family.
_ALIAS_FAMILIES: dict[str, tuple[str, ...]] = {
    "ec2": _EC2_FAMILY,
    "amazon ec2": _EC2_FAMILY,
    "s3": _S3_FAMILY,
    "amazon s3": _S3_FAMILY,
}


@dataclass(frozen=True)
class ServiceResolution:
    """Outcome of resolving requested service names against a snapshot.

    ``resolved`` holds canonical service names in request order, with families
    expanded in their fixed order and duplicates removed. ``missing`` holds the
    original requested strings (untrimmed) that resolved to no present target,
    in request order, so the caller can fail closed with a safe error.
    """

    resolved: tuple[str, ...]
    missing: tuple[str, ...]


def resolve_service_selection(
    requested_names: Sequence[str],
    available_services: Iterable[str],
) -> ServiceResolution:
    """Resolve requested service names to canonical names present in a snapshot.

    Args:
        requested_names: The service names as requested, in request order. Each
            value is trimmed before matching; the original value is preserved
            in ``missing`` when it cannot be resolved.
        available_services: The canonical Cost Explorer service names present in
            the immutable snapshot.

    Returns:
        A :class:`ServiceResolution` whose ``resolved`` tuple lists canonical
        names in request order (families expanded in fixed order, deduplicated)
        and whose ``missing`` tuple lists unresolved original requests.
    """
    by_exact: dict[str, str] = {}
    casefold_candidates: dict[str, set[str]] = {}
    for name in available_services:
        by_exact.setdefault(name, name)
        casefold_candidates.setdefault(name.casefold(), set()).add(name)

    # Only casefold keys that map to exactly one distinct canonical name are
    # usable for case-insensitive matching. When two distinct canonical names
    # collide under casefold the key is ambiguous, so we drop it and fail closed
    # instead of letting snapshot iteration order decide the winner. Exact,
    # case-sensitive matches for either colliding name still resolve via
    # ``by_exact``.
    by_casefold: dict[str, str] = {
        key: next(iter(names)) for key, names in casefold_candidates.items() if len(names) == 1
    }

    resolved: list[str] = []
    seen: set[str] = set()
    missing: list[str] = []

    for original in requested_names:
        trimmed = original.strip()

        # Exact canonical match takes precedence over any alias, first
        # case-sensitively and then case-insensitively.
        canonical = by_exact.get(trimmed)
        if canonical is None:
            canonical = by_casefold.get(trimmed.casefold())

        if canonical is not None:
            targets: tuple[str, ...] = (canonical,)
        else:
            # Fall back to a recognized alias family, keeping only the members
            # present in this snapshot in their fixed deterministic order.
            family = _ALIAS_FAMILIES.get(trimmed.casefold())
            targets = tuple(member for member in family if member in by_exact) if family else ()

        if not targets:
            missing.append(original)
            continue

        for target in targets:
            if target not in seen:
                seen.add(target)
                resolved.append(target)

    return ServiceResolution(resolved=tuple(resolved), missing=tuple(missing))
