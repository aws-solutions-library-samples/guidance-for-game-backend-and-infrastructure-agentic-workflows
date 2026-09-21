"""Combined-package fixture tests for the optional E1 operations Lambda artifact
(GitHub issue #413).

The deploy wrapper packages the `operations` tree from a COMBINED checkout — the
infra worktree merged with issue #413 core, which owns the real
``operations/observe/lambda_entry.py`` handler. The infra worktree itself ships
NO ``operations/observe`` placeholder (that package was deleted so it cannot
add/add-conflict with core). These tests therefore synthesise a *combined-tree
fixture* entirely inside a temp directory — no runtime placeholder source is
added to ``backend/src`` — and assert the packaging invariants the wrapper
depends on:

* a combined tree that contains the real handler passes the handler-present gate,
  and one that lacks it fails closed;
* the deterministic zip (sorted entries, fixed epoch mtime) is byte-stable across
  repeated builds of an unchanged tree, so the content-hash S3 key is stable;
* the structural, fail-closed import probe accepts a package that carries every
  required runtime dependency and rejects one that is missing a dependency;
* the host-native-wheel guard rejects a non-Linux/x86 native wheel.

These never call AWS and never build a real wheel; they exercise the wrapper's
packaging *contract* against a fixture combined tree.
"""

# Standard library
import hashlib
import os
import pathlib
import subprocess
import zipfile

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DEPLOY_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/deploy-operations.sh"
HANDLER_REL = "operations/observe/lambda_entry.py"
EPOCH_STAMP = (2000, 1, 1, 0, 0, 0)

# The importable top-level names of the pinned runtime-dependency closure the
# real handler transitively needs. Mirrors the wrapper's structural probe.
REQUIRED_TOP_LEVEL = ("rfc8785", "jsonschema", "referencing", "rpds")


# --------------------------------------------------------------------------- #
# Fixture builders (temp-only; nothing is written under backend/src)
# --------------------------------------------------------------------------- #
def _write(path: pathlib.Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _combined_operations_tree(root: pathlib.Path) -> pathlib.Path:
    """A minimal COMBINED operations tree: the infra-side modules plus core's
    real observe handler at operations/observe/lambda_entry.py."""
    src = root / "backend" / "src"
    _write(src / "operations" / "__init__.py")
    _write(src / "operations" / "settings.py", "# frozen settings (core)\n")
    _write(src / "operations" / "observe" / "__init__.py")
    _write(
        src / "operations" / "observe" / "lambda_entry.py",
        "def handler(event, context):\n    return {'statusCode': 200}\n",
    )
    return src


def _stage_package(src: pathlib.Path, stage: pathlib.Path, *, with_deps=REQUIRED_TOP_LEVEL) -> None:
    """Stage the operations tree and simulate installed dependency packages.

    Dependency packages are represented by their top-level import directory, as
    the wrapper's structural probe checks for their presence — no real wheels are
    built, keeping the fixture cross-platform and offline.
    """
    stage.mkdir(parents=True, exist_ok=True)
    for py in sorted((src / "operations").rglob("*.py")):
        rel = py.relative_to(src)
        _write(stage / rel, py.read_text(encoding="utf-8"))
    for top in with_deps:
        _write(stage / top / "__init__.py", f"# {top} (fixture stand-in)\n")
    # rpds is native at runtime; represent its compiled extension by filename.
    if "rpds" in with_deps:
        _write(stage / "rpds" / "rpds.cpython-313-x86_64-linux-gnu.so", "")


def _deterministic_zip(stage: pathlib.Path, out: pathlib.Path) -> None:
    """Reproduce the wrapper's determinism: sorted entries + fixed epoch mtime."""
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            info = zipfile.ZipInfo(str(f.relative_to(stage)), date_time=EPOCH_STAMP)
            info.external_attr = 0o644 << 16
            zf.writestr(info, f.read_bytes())


def _structural_probe(stage: pathlib.Path) -> None:
    """Mirror the wrapper's fail-closed structural probe. Raises on failure."""
    if not (stage / HANDLER_REL).is_file():
        raise AssertionError(f"frozen handler {HANDLER_REL} missing from package")
    for required in REQUIRED_TOP_LEVEL:
        hits = (
            list(stage.glob(f"{required}")) + list(stage.glob(f"{required}.py")) + list(stage.glob(f"{required}*.so"))
        )
        if not any(h.exists() for h in hits):
            raise AssertionError(f"required runtime dependency '{required}' absent from package")


# --------------------------------------------------------------------------- #
# Handler-present gate on a combined tree
# --------------------------------------------------------------------------- #
def test_combined_tree_has_real_handler_and_no_repo_placeholder(tmp_path):
    src = _combined_operations_tree(tmp_path)
    assert (src / HANDLER_REL).is_file(), "combined tree must carry core's real handler"
    # The infra repo source must NOT carry a placeholder observe package.
    assert not (
        PROJECT_ROOT / "backend/src/operations/observe"
    ).exists(), "the infra worktree must not ship an operations/observe placeholder"


def test_wrapper_gate_rejects_tree_without_handler(tmp_path):
    """A tree lacking the real handler must be refused (exit 5) before any AWS
    call; the wrapper's handler-present gate encodes this."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    assert 'if [ ! -f "$BACKEND_SRC/$HANDLER_MODULE_PATH" ]; then' in text
    assert "exit 5" in text


# --------------------------------------------------------------------------- #
# Deterministic, content-hash-stable packaging
# --------------------------------------------------------------------------- #
def test_combined_package_zip_is_byte_stable(tmp_path):
    src = _combined_operations_tree(tmp_path)

    stage_a = tmp_path / "stage_a"
    stage_b = tmp_path / "stage_b"
    _stage_package(src, stage_a)
    _stage_package(src, stage_b)

    zip_a = tmp_path / "a.zip"
    zip_b = tmp_path / "b.zip"
    _deterministic_zip(stage_a, zip_a)
    _deterministic_zip(stage_b, zip_b)

    hash_a = hashlib.sha256(zip_a.read_bytes()).hexdigest()
    hash_b = hashlib.sha256(zip_b.read_bytes()).hexdigest()
    assert hash_a == hash_b, "deterministic zip of an unchanged combined tree must be byte-stable"


def test_combined_package_contains_handler_and_deps(tmp_path):
    src = _combined_operations_tree(tmp_path)
    stage = tmp_path / "stage"
    _stage_package(src, stage)
    out = tmp_path / "pkg.zip"
    _deterministic_zip(stage, out)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert HANDLER_REL in names, "package must contain the real handler"
    for required in REQUIRED_TOP_LEVEL:
        assert any(n.split("/")[0] == required for n in names), f"package must contain dependency {required}"


# --------------------------------------------------------------------------- #
# Structural import probe: accepts complete package, rejects incomplete
# --------------------------------------------------------------------------- #
def test_structural_probe_accepts_complete_combined_package(tmp_path):
    src = _combined_operations_tree(tmp_path)
    stage = tmp_path / "stage"
    _stage_package(src, stage)
    _structural_probe(stage)  # must not raise


def test_structural_probe_fails_closed_on_missing_dependency(tmp_path):
    src = _combined_operations_tree(tmp_path)
    stage = tmp_path / "stage"
    # Omit rfc8785 to simulate a broken cross-platform install.
    _stage_package(src, stage, with_deps=("jsonschema", "referencing", "rpds"))
    with pytest.raises(AssertionError, match="rfc8785"):
        _structural_probe(stage)


def test_host_native_wheel_guard_rejects_non_linux_x86(tmp_path):
    """The wrapper aborts if a host/non-x86_64 native wheel was staged. Reproduce
    the guard's filename test against a macOS/arm64 .so."""
    src = _combined_operations_tree(tmp_path)
    stage = tmp_path / "stage"
    _stage_package(src, stage)
    # Simulate a host wheel leaking in.
    _write(stage / "rpds" / "rpds.cpython-313-darwin.so", "")
    bad = [
        p
        for p in stage.rglob("*.so")
        if any(tag in p.name for tag in ("macosx", "arm64", "aarch64", "win_", "darwin", "_i686"))
    ]
    assert bad, "the guard must detect a host/non-x86_64 native wheel"
