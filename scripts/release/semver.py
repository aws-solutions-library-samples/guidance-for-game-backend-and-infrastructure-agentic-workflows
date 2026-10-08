"""Pre-1.0 release tag grammar and semantic-version precedence.

The accepted grammar is intentionally narrow:

* ``vMAJOR.MINOR.PATCH`` (a stable milestone), or
* ``vMAJOR.MINOR.PATCH-rc.N`` (a release candidate prerelease).

No leading zeros, no build metadata (``+...``), no whitespace, and no
alternative prerelease identifiers. The grammar is anchored so an attacker
cannot smuggle shell, newline, ref, or path characters through the ``tag``
dispatch input.
"""

from __future__ import annotations

# Standard library
import re
from dataclasses import dataclass

# Anchored, no leading zeros, optional ``-rc.N`` prerelease where N has no
# leading zero. ``\Z`` (not ``$``) so a trailing newline cannot slip past.
_TAG_RE = re.compile(
    r"\Av"
    r"(?P<major>0|[1-9][0-9]*)"
    r"\.(?P<minor>0|[1-9][0-9]*)"
    r"\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-rc\.(?P<rc>0|[1-9][0-9]*))?"
    r"\Z"
)


class InvalidTagError(ValueError):
    """Raised when a proposed tag does not match the release grammar."""


@dataclass(frozen=True, order=False)
class ReleaseTag:
    """A parsed, validated release tag with semver precedence support."""

    raw: str
    major: int
    minor: int
    patch: int
    rc: int | None

    @property
    def is_prerelease(self) -> bool:
        return self.rc is not None

    def _precedence_key(self) -> tuple[int, int, int, int, int]:
        # Per semver 11: a prerelease has lower precedence than the associated
        # normal version. Encode "no prerelease" as a higher final tuple so
        # ``v0.1.0`` > ``v0.1.0-rc.9``.
        if self.rc is None:
            return (self.major, self.minor, self.patch, 1, 0)
        return (self.major, self.minor, self.patch, 0, self.rc)

    def __lt__(self, other: "ReleaseTag") -> bool:
        if not isinstance(other, ReleaseTag):
            return NotImplemented
        return self._precedence_key() < other._precedence_key()

    def __gt__(self, other: "ReleaseTag") -> bool:
        if not isinstance(other, ReleaseTag):
            return NotImplemented
        return self._precedence_key() > other._precedence_key()


def parse_tag(value: str) -> ReleaseTag:
    """Parse ``value`` into a :class:`ReleaseTag` or raise :class:`InvalidTagError`.

    ``value`` must be an exact string match for the grammar. Any surrounding
    whitespace, control character, build metadata, or injection attempt is a
    hard rejection rather than something to normalize away.
    """

    if not isinstance(value, str):
        raise InvalidTagError("tag must be a string")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise InvalidTagError("tag must not contain whitespace")
    match = _TAG_RE.match(value)
    if match is None:
        raise InvalidTagError(f"tag does not match vMAJOR.MINOR.PATCH[-rc.N]: {value!r}")
    rc_text = match.group("rc")
    return ReleaseTag(
        raw=value,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        rc=int(rc_text) if rc_text is not None else None,
    )


def is_valid_tag(value: str) -> bool:
    try:
        parse_tag(value)
    except InvalidTagError:
        return False
    return True


def strictly_greater_than_all(candidate: str, existing: list[str]) -> bool:
    """Return True iff ``candidate`` is strictly greater than every parseable
    existing ``v*`` tag. Existing tags that do not parse under our grammar are
    ignored for precedence (they cannot have been produced by this workflow),
    but a candidate equal to any existing tag is never greater.
    """

    parsed_candidate = parse_tag(candidate)
    for name in existing:
        try:
            parsed_existing = parse_tag(name)
        except InvalidTagError:
            continue
        if not parsed_candidate > parsed_existing:
            return False
    return True
