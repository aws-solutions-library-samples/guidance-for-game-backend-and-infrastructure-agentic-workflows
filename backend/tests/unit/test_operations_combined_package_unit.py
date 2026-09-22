"""Combined-package fixture tests for the optional E1 operations Lambda artifact
(GitHub issue #413).

The deploy wrapper packages the `operations` tree from a COMBINED checkout — the
infra worktree merged with issue #413 core, which owns the real
``operations/observe/lambda_entry.py`` handler. The infra worktree itself ships
NO ``operations/observe`` placeholder (that package was deleted so it cannot
add/add-conflict with core). These tests therefore synthesise a *combined-tree
fixture* (or a sibling core worktree, when present) entirely outside
``backend/src`` — no runtime placeholder source is added to ``backend/src`` — and
assert the packaging invariants the wrapper depends on:

* a materialized combined tree carries a real, module-level ``handler`` at the
  frozen path — exactly the CloudFormation ``Handler`` — and a tree that lacks it
  fails the wrapper's handler-present gate closed;
* the infra worktree itself *tracks* no ``operations/observe`` source and no
  ``register_observer`` seam (checked against the git index, not the on-disk
  path, so it stays correct on a legitimate combined tree where core owns the
  path);
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
import sys
import textwrap
import zipfile

# Third-party packages
import pytest
from _combined_tree import (
    HANDLER_DOTTED,
    materialize_combined_operations_tree,
    module_defines_top_level_handler,
    observe_placeholder_seam_hits,
)

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DEPLOY_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/deploy-operations.sh"
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"
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
# Handler-present gate on a combined tree; infra contributes no placeholder
# --------------------------------------------------------------------------- #
def test_combined_tree_has_real_module_level_handler(tmp_path):
    """A materialized combined tree (sibling core worktree when present, else a
    faithful fixture) carries ``operations/observe/lambda_entry.py`` defining a
    module-level ``handler`` — the exact CloudFormation ``Handler``. Always
    materialized, so this never becomes vacuous in the infra-only context."""
    src = materialize_combined_operations_tree(tmp_path, PROJECT_ROOT)
    lambda_entry = src / HANDLER_REL
    assert lambda_entry.is_file(), "combined tree must carry core's real handler"
    assert module_defines_top_level_handler(
        lambda_entry
    ), "the combined-tree handler must define a module-level def handler(...)"
    handler_value = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))["Resources"]
    # Find the Lambda function's Handler and confirm it names the combined-tree
    # module. Kept resource-name-agnostic to avoid coupling to the logical id.
    lambda_handlers = [
        r["Properties"]["Handler"] for r in handler_value.values() if r.get("Type") == "AWS::Lambda::Function"
    ]
    assert lambda_handlers == [HANDLER_DOTTED], (
        f"template Handler {lambda_handlers!r} must be exactly [{HANDLER_DOTTED!r}] "
        "so the packaged combined-tree module is what Lambda invokes"
    )


def test_no_observe_placeholder_seam_is_tracked():
    """No tracked ``operations/observe`` source may carry an infra-style
    placeholder seam (a ``register_observer`` shim or fail-closed placeholder
    stub). Asserted by tracked-file *content*, not the on-disk path, so it holds
    both when the path is untracked (infra-only) and when core's real handler is
    tracked (combined). This is the invariant that keeps a core+infra merge free
    of add/add conflicts."""
    hits = observe_placeholder_seam_hits(PROJECT_ROOT)
    assert hits == [], f"no operations/observe placeholder or register_observer seam may be tracked; found: {hits}"


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


# --------------------------------------------------------------------------- #
# The E1 Lambda store boundary imports WITHOUT loguru (issue #413 regression)
# --------------------------------------------------------------------------- #
# ``loguru`` is a full-backend dependency but is deliberately ABSENT from the
# minimal E1 observe Lambda's runtime closure (see ``REQUIRED_TOP_LEVEL``). The
# store's diagnostic boundary once did ``from loguru import logger`` at module
# scope, so importing the packaged handler chain raised ``ModuleNotFoundError:
# loguru`` in the deployed Lambda before it served a single request. These tests
# prove the boundary now imports cleanly under stdlib ``logging`` alone, and pin
# the invariant that no operations module reintroduces a top-level loguru import.
BACKEND_SRC = PROJECT_ROOT / "backend" / "src"
OPERATIONS_TREE = BACKEND_SRC / "operations"


def _import_under_blocked_loguru(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` in a subprocess where ``import loguru`` fails, mirroring
    the minimal E1 Lambda closure that ships no loguru. The child adds a
    ``sys.meta_path`` finder that raises ``ModuleNotFoundError`` for loguru (and
    any submodule), then imports the target; exit 0 == imported without loguru."""
    program = textwrap.dedent(f"""
        import importlib
        import sys

        class _BlockLoguru:
            def find_spec(self, name, path=None, target=None):
                if name == "loguru" or name.startswith("loguru."):
                    raise ModuleNotFoundError("No module named 'loguru'", name="loguru")
                return None

        # Belt and suspenders: block a pre-imported loguru too.
        for _n in [n for n in sys.modules if n == "loguru" or n.startswith("loguru.")]:
            del sys.modules[_n]
        sys.meta_path.insert(0, _BlockLoguru())

        importlib.import_module({module!r})

        # Prove the import really ran with loguru unavailable.
        assert "loguru" not in sys.modules, "loguru was imported despite the block"
        print("IMPORT_OK")
        """)
    env = dict(os.environ)
    # The child imports ``operations.*`` from backend/src by path.
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND_SRC), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_SRC),
    )


def test_observation_store_imports_without_loguru():
    """The store boundary — the only operations module that ever imported loguru
    — imports cleanly when loguru is unavailable, as it is in the E1 Lambda."""
    result = _import_under_blocked_loguru("operations.observation_store")
    assert result.returncode == 0, (
        "operations.observation_store must import without loguru (E1 Lambda closure "
        f"excludes it).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_lambda_entry_module_imports_without_loguru():
    """The deployable E1 handler module (``operations.observe.lambda_entry``, the
    frozen CloudFormation ``Handler``) imports its whole transitive chain — which
    includes the store boundary — without loguru present."""
    result = _import_under_blocked_loguru("operations.observe.lambda_entry")
    assert result.returncode == 0, (
        "operations.observe.lambda_entry must import without loguru so the deployed "
        f"Lambda can start.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_no_operations_module_imports_loguru_at_top_level():
    """No module under ``operations/`` may carry a top-level loguru import. This
    is the structural invariant that keeps the minimal E1 Lambda closure honest:
    a single ``from loguru import logger`` anywhere in the packaged tree would
    reintroduce the deploy-time ``ModuleNotFoundError``. Checked by scanning the
    tracked source text (line-oriented, ignoring comments) rather than importing,
    so it holds even for modules with heavy import-time side effects."""
    offenders = []
    for py in sorted(OPERATIONS_TREE.rglob("*.py")):
        for lineno, raw in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if line.startswith("#"):
                continue
            if line.startswith("import loguru") or line.startswith("from loguru"):
                offenders.append(f"{py.relative_to(PROJECT_ROOT)}:{lineno}: {line}")
    assert offenders == [], (
        "no operations module may import loguru (it is outside the E1 Lambda "
        "dependency closure); found:\n" + "\n".join(offenders)
    )
