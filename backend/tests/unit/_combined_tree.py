"""Shared helpers for the E1 operations packaging/contract tests (issue #413).

The E1 work is split across two worktrees that merge into one deployable tree:

* the **infra** worktree (this one) owns the CloudFormation template, the
  deploy/teardown wrappers, and the packaging contract. It deliberately ships
  **no** ``operations/observe`` handler — not even a placeholder ``handler`` or a
  ``register_observer`` seam — so it cannot add/add-conflict with core on merge.
* the **core** worktree owns the single real, deployable handler at
  ``operations/observe/lambda_entry.py`` (a module-level ``handler(event,
  context)``), which is exactly the CloudFormation ``Handler`` and the module the
  wrapper packages and import-probes.

The packaging wrapper runs against the **combined** tree. Two properties are
asserted, each designed to stay correct in *both* the infra-only context and a
materialized combined-tree context (never vacuous, never wrong):

1. **No placeholder/register_observer seam.** The ownership invariant is about
   *what infra contributes*, not *whether the path exists*. The earlier tests
   asserted the on-disk path was absent, which is false on a legitimate combined
   tree where core owns and materializes ``operations/observe``. Instead,
   :func:`observe_placeholder_seam_hits` scans the *tracked* ``operations/observe``
   source for the placeholder signatures (a ``register_observer`` seam or a
   fail-closed placeholder stub). It returns nothing when the path is untracked
   (infra-only) AND when the tracked files are core's real handler (combined),
   and returns hits only if an infra-style placeholder was reintroduced.

2. **Combined tree carries the real handler.** :func:`materialize_combined_operations_tree`
   always produces a tree whose ``operations/observe/lambda_entry.py`` defines a
   module-level ``handler`` — preferring a sibling core worktree's real handler,
   else a faithful fixture — so the positive combined-tree assertions always run.
"""

# Standard library
import pathlib
import subprocess

# The frozen handler module path/import the template Handler and the wrapper use.
HANDLER_REL = "operations/observe/lambda_entry.py"
HANDLER_IMPORT = "operations.observe.lambda_entry"
HANDLER_DOTTED = "operations.observe.lambda_entry.handler"

# Signatures that mark an infra-style *placeholder* handler (never present in
# core's real, deployable module). Any tracked observe source carrying one of
# these means the deleted infra placeholder/register_observer seam was
# reintroduced — an add/add-conflict risk on merge.
PLACEHOLDER_SEAM_MARKERS = (
    "register_observer",
    "core observation implementation is registered",
    "fails closed with a typed 503 until",
)

# A faithful stand-in for core's real handler: a module-level ``handler`` with a
# ``(event, context)`` signature. This is NOT a fail-closed placeholder and it
# carries no ``register_observer`` seam — it stands in for the deployable core
# module in the infra-only context so the combined-tree contract is exercised.
_FIXTURE_LAMBDA_ENTRY = (
    '"""Combined-tree fixture standing in for core\'s real observe handler."""\n'
    "\n"
    "\n"
    "def handler(event, context=None):\n"
    '    """Module-level Lambda entry point (matches core\'s real signature)."""\n'
    "    return {'statusCode': 200}\n"
)


def _write(path: pathlib.Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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

    Empty in both intended contexts:

    * infra-only — nothing is tracked under ``operations/observe``; and
    * combined — the tracked files are core's real handler, which carries no
      placeholder seam.

    Non-empty only if an infra-style placeholder was (re)introduced.
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


def _sibling_core_handler(infra_root: pathlib.Path) -> pathlib.Path | None:
    """Locate a real core handler in a sibling core worktree, if one exists.

    Deployments materialize a combined tree; developers keep core in a sibling
    ``logs/issue-413-core`` worktree. Prefer that real handler when present so
    the combined-tree assertions run against the genuine core module.
    """
    candidates = [
        infra_root.parent / "issue-413-core" / "backend" / "src" / HANDLER_REL,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def combined_tree_handler_source(repo_root: pathlib.Path) -> pathlib.Path | None:
    """If ``repo_root`` itself already IS a combined tree (core's real handler is
    present on disk under ``backend/src``), return that handler path; else None.

    Used so the combined-tree assertions bind to the real handler when the suite
    is run inside a materialized combined checkout.
    """
    on_disk = repo_root / "backend" / "src" / HANDLER_REL
    if on_disk.is_file():
        return on_disk
    return None


def materialize_combined_operations_tree(
    tmp_root: pathlib.Path,
    infra_root: pathlib.Path,
) -> pathlib.Path:
    """Return ``backend/src`` of a combined operations tree carrying a real
    module-level handler at :data:`HANDLER_REL`.

    Resolution order for the real handler:

    1. ``infra_root`` is itself a combined tree (core's handler already on disk);
    2. a sibling core worktree's real handler; else
    3. a faithful synthesized fixture.

    In every case the returned tree defines a module-level ``handler``, so the
    combined-tree contract is always exercised and never becomes vacuous.
    """
    src = tmp_root / "backend" / "src"
    _write(src / "operations" / "__init__.py")
    _write(src / "operations" / "settings.py", "# frozen settings (core-owned in merge)\n")
    _write(src / "operations" / "observe" / "__init__.py")

    real = combined_tree_handler_source(infra_root) or _sibling_core_handler(infra_root)
    if real is not None:
        _write(
            src / HANDLER_REL,
            "# vendored from the real core handler for the combined-tree contract\n\n"
            + real.read_text(encoding="utf-8"),
        )
    else:
        _write(src / HANDLER_REL, _FIXTURE_LAMBDA_ENTRY)
    return src


def module_defines_top_level_handler(lambda_entry: pathlib.Path) -> bool:
    """True iff the module defines a module-level ``def handler(...)`` (the
    signature AWS Lambda invokes), not merely a nested/placeholder reference."""
    text = lambda_entry.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("def handler(") or line.startswith("def handler "):
            return True
    return False
