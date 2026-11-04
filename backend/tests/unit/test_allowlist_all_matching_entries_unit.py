#!/usr/bin/env python3
"""Unit test for PR #319 review finding #5 — check ALL matching allowlist entries.

The seven-dimension :class:`~connector.config.AuthorizationPolicy` must not select the FIRST
repo+branch-matching entry and then apply only that entry's path/extension constraints.
When an operator lists several entries for the SAME repository+branch that each scope a
different set of paths/extensions, the request is permitted iff **any** matching entry
permits all requested paths and extensions.

Validates: PR #319 review finding 5.
"""

# Third-party packages
import pytest

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"
_GROUPS = ["scm-writers"]
_AUTHORIZED = ["scm-writers"]


def _authorize(entries, paths):
    """Authorize a read of ``paths`` on org/iac@main for an authorized requester."""
    policy = AuthorizationPolicy(entries=tuple(entries))
    return policy.authorize(
        tenant="",
        workspace="",
        repo=_REPO,
        branch=_BRANCH,
        paths=paths,
        groups=_GROUPS,
        authorized_groups=_AUTHORIZED,
    )


def test_second_matching_entry_permits_when_first_does_not():
    """Two entries for the same repo+branch: only the SECOND permits the requested path.

    The first entry scopes ``infra/`` only; the second scopes ``modules/``. A request for a
    ``modules/`` path must be PERMITTED because the second matching entry permits it — the
    policy must not stop at the first (``infra/``) entry and deny.
    """
    entries = [
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=("infra/",)),
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=("modules/",)),
    ]

    decision = _authorize(entries, ["modules/vpc.tf"])

    assert decision.allowed is True, "a modules/ path must be permitted by the second matching entry"
    assert decision.repo == _REPO
    assert decision.branch == _BRANCH


def test_second_matching_entry_permits_by_extension():
    """The extension dimension is likewise satisfied by ANY matching entry.

    First entry permits only ``.yaml``; second permits only ``.tf``. A ``.tf`` request must
    be permitted by the second entry rather than denied on the first entry's extension set.
    """
    entries = [
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), extensions=(".yaml",)),
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), extensions=(".tf",)),
    ]

    decision = _authorize(entries, ["main.tf"])

    assert decision.allowed is True, "a .tf path must be permitted by the second matching entry"


def test_request_denied_when_no_matching_entry_permits():
    """When NO matching entry permits the path, the request is still denied on 'path'."""
    entries = [
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=("infra/",)),
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=("modules/",)),
    ]

    decision = _authorize(entries, ["secrets/creds.txt"])

    assert decision.allowed is False
    assert decision.failed_dimension == "path"


def test_extension_dimension_reported_when_path_passes_but_extension_fails():
    """If some entry passes the path check but none passes the extension check, report 'extension'."""
    entries = [
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=("infra/",), extensions=(".yaml",)),
        AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=("infra/",), extensions=(".json",)),
    ]

    decision = _authorize(entries, ["infra/main.tf"])

    assert decision.allowed is False
    assert decision.failed_dimension == "extension"
