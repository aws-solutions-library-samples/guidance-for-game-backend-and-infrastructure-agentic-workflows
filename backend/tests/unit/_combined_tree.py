"""Shared helpers for the E1 operations packaging/contract tests (issue #413).

This module backs the E1 operations packaging/contract tests, which run against
the single combined deployable tree:

* the **CloudFormation template**, the deploy/teardown wrappers, and the
  packaging contract own the infrastructure side. The infrastructure side ships
  **no** ``operations/observe`` handler — not even a placeholder ``handler`` or a
  ``register_observer`` seam — so it never add/add-conflicts with the core
  backend when the two sides are composed.
* the **core backend** owns the single real, deployable handler at
  ``operations/observe/lambda_entry.py`` (a module-level ``handler(event,
  context)``), which is exactly the CloudFormation ``Handler`` and the module the
  wrapper packages and import-probes.

The packaging wrapper runs against the single deployable tree. Two properties
are asserted:

1. **No placeholder/register_observer seam.** The ownership invariant is about
   what the infrastructure side contributes, not *whether the path exists*.
   Asserting the on-disk path is absent would be false on this tree, where the
   core backend owns and materializes ``operations/observe``. Instead,
   :func:`observe_placeholder_seam_hits` scans the *tracked*
   ``operations/observe`` source for placeholder signatures (a
   ``register_observer`` seam or a fail-closed placeholder stub). It returns
   nothing when the tracked files are the core backend's real handler, and
   returns hits only if an infrastructure-style placeholder is present.

2. **Deployable tree carries the real handler.** :func:`materialize_combined_operations_tree`
   produces a tree whose ``operations/observe/lambda_entry.py`` is the real,
   on-disk handler, so the positive assertions bind to the deployed module.
"""

# Standard library
import pathlib
import subprocess

# The frozen handler module path/import the template Handler and the wrapper use.
HANDLER_REL = "operations/observe/lambda_entry.py"
HANDLER_IMPORT = "operations.observe.lambda_entry"
HANDLER_DOTTED = "operations.observe.lambda_entry.handler"

# The deployed E1 store module (relative to ``backend/src``). This is the module
# the CloudFormation Lambda's dependency closure loads, and the module the IAM
# drift guard drives to derive the DynamoDB actions the store actually requires.
STORE_MODULE_REL = "operations/observation_store.py"

# Signatures that mark an infra-style *placeholder* handler, which the core
# backend's real, deployable module never carries. Any tracked observe source
# carrying one of these means an infrastructure-style placeholder or
# ``register_observer`` seam is present where only the real handler belongs.
PLACEHOLDER_SEAM_MARKERS = (
    "register_observer",
    "core observation implementation is registered",
    "fails closed with a typed 503 until",
)


# A module-level ``handler`` with a ``(event, context)`` signature marks the
# real, deployable core module.
def _write(path: pathlib.Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def in_git_work_tree(repo_root: pathlib.Path) -> bool:
    """True when ``repo_root`` is inside a git work tree.

    The seam checks read the git index; outside a checkout (for example a
    ``git archive`` extraction) ``git`` fails, so callers skip rather than treat
    the failure as a result.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_tracked_files(repo_root: pathlib.Path, pathspec: str) -> list[str]:
    """Files tracked in ``repo_root``'s index matching ``pathspec``.

    Returns ``[]`` when the pathspec matches nothing. Raises on git failure so a
    broken invocation can never masquerade as "no tracked placeholder".
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", pathspec],
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


def _git_show(repo_root: pathlib.Path, rel_path: str) -> str:
    """Contents of a tracked file from the index (``:path``), so the check reads
    what is *committed/staged*, independent of any dirty worktree edits."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f":{rel_path}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def observe_placeholder_seam_hits(repo_root: pathlib.Path) -> list[str]:
    """Tracked ``operations/observe`` files that carry an infra-style placeholder
    seam (``register_observer`` or a fail-closed placeholder stub).

    Empty in the intended context: the tracked files are the core backend's real
    handler, which carries no placeholder seam. Non-empty only if an infra-style
    placeholder is present.
    """
    hits: list[str] = []
    for rel in _git_tracked_files(repo_root, "backend/src/operations/observe/**"):
        try:
            text = _git_show(repo_root, rel)
        except subprocess.CalledProcessError:
            # Tracked but unreadable from the index: treat as suspicious.
            hits.append(f"{rel}: <unreadable from index>")
            continue
        for marker in PLACEHOLDER_SEAM_MARKERS:
            if marker in text:
                hits.append(f"{rel}: contains placeholder marker {marker!r}")
    return hits


def combined_tree_handler_source(repo_root: pathlib.Path) -> pathlib.Path | None:
    """Return the real on-disk handler path under ``repo_root``'s ``backend/src``,
    or ``None`` if it is absent.

    Used so the deployable-tree assertions bind to the real handler.
    """
    on_disk = repo_root / "backend" / "src" / HANDLER_REL
    if on_disk.is_file():
        return on_disk
    return None


def resolve_deployed_store_src(repo_root: pathlib.Path) -> pathlib.Path | None:
    """Return the ``backend/src`` that contains the deployed E1 store module, or
    ``None`` if it cannot be located.

    ``repo_root``'s ``backend/src`` ships the deployed store module (and the
    deployed handler), so the IAM drift guard binds to the same module the
    packaging contract packages, and the guard executes non-vacuously.
    """
    src = repo_root / "backend" / "src"
    if (src / STORE_MODULE_REL).is_file():
        return src
    return None


def materialize_combined_operations_tree(
    tmp_root: pathlib.Path,
    infra_root: pathlib.Path,
) -> pathlib.Path:
    """Return ``backend/src`` of a deployable operations tree carrying the real
    module-level handler at :data:`HANDLER_REL`.

    The real handler at ``infra_root``'s ``backend/src`` is vendored into the
    tree. A missing handler is a hard failure, so the deployable-tree contract
    can never pass against a synthesized stand-in.
    """
    src = tmp_root / "backend" / "src"
    _write(src / "operations" / "__init__.py")
    _write(src / "operations" / "settings.py", "# frozen settings (core-owned)\n")
    _write(src / "operations" / "observe" / "__init__.py")

    real = combined_tree_handler_source(infra_root)
    if real is None:
        raise AssertionError(
            f"the real observe handler is absent under {infra_root}/backend/src/{HANDLER_REL}; "
            "the deployable-tree contract requires the real handler, not a stand-in"
        )
    _write(
        src / HANDLER_REL,
        "# vendored from the real core handler for the deployable-tree contract\n\n" + real.read_text(encoding="utf-8"),
    )
    return src


def module_defines_top_level_handler(lambda_entry: pathlib.Path) -> bool:
    """True iff the module defines a module-level ``def handler(...)`` (the
    signature AWS Lambda invokes), not merely a nested/placeholder reference."""
    text = lambda_entry.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("def handler(") or line.startswith("def handler "):
            return True
    return False
