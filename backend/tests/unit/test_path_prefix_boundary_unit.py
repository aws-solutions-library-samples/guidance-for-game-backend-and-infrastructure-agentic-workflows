"""Regression for PR #319 re-review — path-allowlist prefixes are directory-boundary aware.

The path dimension of :class:`~connector.config.AuthorizationPolicy` matches a requested
path against an entry's ``path_prefixes``. A bare ``str.startswith`` let a configured
prefix such as ``infra`` (or the documented ``infra/``) authorize the *sibling*
``infra-secrets/prod.tf``, because ``"infra-secrets/prod.tf".startswith("infra")`` is true.
This is a security allowlist boundary, so the match must only admit a path that equals the
prefix or lies beneath it at a ``/`` separator.

These tests prove:
- a prefix ``infra`` (and ``infra/``) does NOT authorize ``infra-secrets/prod.tf``;
- the documented ``infra/`` form still authorizes ``infra/main.tf`` and nested paths;
- a bare ``infra`` behaves like the directory prefix ``infra/``;
- an exact-file prefix matches only that file, not a longer sibling name.

Validates: PR #319 re-review finding 1 (directory-boundary path allowlist).
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


def _authorize(prefixes, paths):
    """Authorize a read of ``paths`` on org/iac@main for an entry scoped to ``prefixes``."""
    policy = AuthorizationPolicy(
        entries=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,), path_prefixes=tuple(prefixes)),)
    )
    return policy.authorize(
        tenant="",
        workspace="",
        repo=_REPO,
        branch=_BRANCH,
        paths=paths,
        groups=_GROUPS,
        authorized_groups=_AUTHORIZED,
    )


@pytest.mark.parametrize("prefix", ["infra", "infra/"])
def test_prefix_does_not_authorize_sibling_directory(prefix):
    """``infra`` / ``infra/`` must NOT authorize the sibling ``infra-secrets/prod.tf``."""
    decision = _authorize([prefix], ["infra-secrets/prod.tf"])

    assert decision.allowed is False, f"prefix {prefix!r} must not admit a sibling directory"
    assert decision.failed_dimension == "path"


@pytest.mark.parametrize("prefix", ["infra", "infra/"])
def test_prefix_authorizes_paths_under_the_directory(prefix):
    """Both the documented ``infra/`` and a bare ``infra`` authorize files under ``infra/``."""
    decision = _authorize([prefix], ["infra/main.tf"])

    assert decision.allowed is True, f"prefix {prefix!r} must admit files directly under it"
    assert decision.repo == _REPO
    assert decision.branch == _BRANCH


@pytest.mark.parametrize("prefix", ["infra", "infra/"])
def test_prefix_authorizes_deeply_nested_paths(prefix):
    """A directory prefix admits nested paths beneath it."""
    decision = _authorize([prefix], ["infra/modules/vpc/main.tf"])

    assert decision.allowed is True


def test_all_requested_paths_must_lie_under_the_prefix():
    """A batch is denied when any path escapes the directory boundary."""
    decision = _authorize(["infra/"], ["infra/main.tf", "infra-secrets/prod.tf"])

    assert decision.allowed is False
    assert decision.failed_dimension == "path"


def test_exact_file_prefix_matches_only_that_file():
    """An exact-file prefix admits that file but not a longer sibling name."""
    allowed = _authorize(["infra/main.tf"], ["infra/main.tf"])
    assert allowed.allowed is True

    denied = _authorize(["infra/main.tf"], ["infra/main.tf.bak"])
    assert denied.allowed is False
    assert denied.failed_dimension == "path"
