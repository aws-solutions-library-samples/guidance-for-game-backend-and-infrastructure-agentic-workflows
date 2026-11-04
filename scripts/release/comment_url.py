"""Parse and validate the ``evidence_comment_url`` dispatch input.

The accepted form is a GitHub issue-comment permalink in *this* repository:

    https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<comment_id>

The parser is strict and anchored so a hostile input cannot redirect the
workflow at another repository or smuggle characters into later steps.
"""

from __future__ import annotations

# Standard library
import re
from dataclasses import dataclass

_URL_RE = re.compile(
    r"\Ahttps://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]{0,99})/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]{0,99})/"
    r"issues/(?P<issue>[1-9][0-9]{0,18})"
    r"#issuecomment-(?P<comment_id>[1-9][0-9]{0,18})"
    r"\Z"
)


class CommentUrlError(ValueError):
    """Raised when the evidence comment URL is malformed or cross-repository."""


@dataclass(frozen=True)
class CommentRef:
    owner: str
    repo: str
    issue: int
    comment_id: int


def parse_comment_url(url: str, *, expected_owner: str, expected_repo: str) -> CommentRef:
    if not isinstance(url, str) or url != url.strip() or any(c.isspace() for c in url):
        raise CommentUrlError("evidence comment url must be a single trimmed token")
    match = _URL_RE.match(url)
    if match is None:
        raise CommentUrlError("evidence comment url is not a valid issue-comment permalink")
    owner = match.group("owner")
    repo = match.group("repo")
    if owner.lower() != expected_owner.lower() or repo.lower() != expected_repo.lower():
        raise CommentUrlError("evidence comment url points at a different repository")
    return CommentRef(
        owner=owner,
        repo=repo,
        issue=int(match.group("issue")),
        comment_id=int(match.group("comment_id")),
    )
