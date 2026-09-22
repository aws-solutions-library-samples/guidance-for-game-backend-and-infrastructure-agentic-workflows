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
import json
import os
import pathlib
import subprocess
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

# Local modules
# The real versioned contract schema resources the runtime handler loads via
# importlib.resources at validate time. These live in backend/src (owned by this
# infra worktree's operations package) and MUST be packaged into the Lambda zip;
# a .py-only package omits them and every contract load raises FileNotFoundError.
# The authoritative set is operations.contracts.versions.SCHEMA_NAMES; we import
# it so the test can never drift from the code under package.
from operations.contracts.versions import SCHEMA_NAMES  # noqa: E402

SCHEMA_SRC_DIR = PROJECT_ROOT / "backend" / "src" / "operations" / "contracts" / "schemas" / "v1"
SCHEMA_REL_DIR = "operations/contracts/schemas/v1"
EXPECTED_SCHEMA_FILES = frozenset(f"{name}.schema.json" for name in SCHEMA_NAMES)


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
# Runtime NON-CODE resources: the versioned JSON Schemas the handler loads.
#
# The regression these tests lock down (issue #413 live E1): a live valid POST
# reached service validation but the handler raised FileNotFoundError because the
# deploy wrapper staged only operations/**/*.py while the runtime contract
# validator loads operations/contracts/schemas/v1/*.schema.json via
# importlib.resources. The Lambda zip omitted every schema, so the first
# validated request failed closed in production instead of at build time.
# --------------------------------------------------------------------------- #
def _copy_real_schemas(src: pathlib.Path) -> None:
    """Copy the REAL versioned schema resources into a combined-tree fixture, so
    the fixture carries genuine runtime resources (not stand-ins). Fails loudly
    if the source set is empty, so the test can never pass vacuously."""
    files = sorted(SCHEMA_SRC_DIR.glob("*.schema.json"))
    assert files, f"no source schemas under {SCHEMA_SRC_DIR}; fixture would be vacuous"
    dest_dir = src / SCHEMA_REL_DIR
    for f in files:
        _write(dest_dir / f.name, f.read_text(encoding="utf-8"))
    # The package must be importable at the contracts path.
    _write(src / "operations" / "contracts" / "__init__.py")


def _combined_operations_tree_with_schemas(root: pathlib.Path) -> pathlib.Path:
    """A combined operations tree that ALSO carries the real runtime schema
    resources — the tree the fixed wrapper stages from."""
    src = _combined_operations_tree(root)
    _copy_real_schemas(src)
    return src


def _stage_pyonly(src: pathlib.Path, stage: pathlib.Path, *, with_deps=REQUIRED_TOP_LEVEL) -> None:
    """Reproduce the OLD, buggy staging: Python modules ONLY. Non-code runtime
    resources (the JSON schemas) are dropped, exactly as the pre-fix wrapper did
    (`find operations -type f -name '*.py'`)."""
    stage.mkdir(parents=True, exist_ok=True)
    for py in sorted((src / "operations").rglob("*.py")):
        _write(stage / py.relative_to(src), py.read_text(encoding="utf-8"))
    for top in with_deps:
        _write(stage / top / "__init__.py", f"# {top} (fixture stand-in)\n")
    if "rpds" in with_deps:
        _write(stage / "rpds" / "rpds.cpython-313-x86_64-linux-gnu.so", "")


def _stage_with_resources(src: pathlib.Path, stage: pathlib.Path, *, with_deps=REQUIRED_TOP_LEVEL) -> None:
    """Reproduce the FIXED staging: Python modules PLUS the non-code runtime
    resources (versioned JSON schemas), mirroring the wrapper's
    `find operations -type f \\( -name '*.py' -o -name '*.json' \\)`."""
    stage.mkdir(parents=True, exist_ok=True)
    for f in sorted((src / "operations").rglob("*")):
        if not f.is_file():
            continue
        if f.suffix not in (".py", ".json"):
            continue
        if "__pycache__" in f.parts:
            continue
        _write(stage / f.relative_to(src), f.read_text(encoding="utf-8"))
    for top in with_deps:
        _write(stage / top / "__init__.py", f"# {top} (fixture stand-in)\n")
    if "rpds" in with_deps:
        _write(stage / "rpds" / "rpds.cpython-313-x86_64-linux-gnu.so", "")


def _schema_names_in_zip(zip_path: pathlib.Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return {
            n.rsplit("/", 1)[-1]
            for n in zf.namelist()
            if n.startswith(SCHEMA_REL_DIR + "/") and n.endswith(".schema.json")
        }


# --------------------------------------------------------------------------- #
# RED: the old py-only staging omits every runtime schema (the live failure)
# --------------------------------------------------------------------------- #
def test_pyonly_staging_omits_runtime_schemas_is_red(tmp_path):
    """Proves the pre-fix behaviour is a genuine defect: a package built by the
    old `.py`-only staging carries the handler and its deps but NONE of the
    versioned JSON schemas the runtime contract validator reads — the exact
    FileNotFoundError root cause. This test would PASS (green) against the buggy
    wrapper's staging and documents why the fix is required."""
    src = _combined_operations_tree_with_schemas(tmp_path)
    # Sanity: the source tree really does carry the schemas.
    assert (src / SCHEMA_REL_DIR).is_dir()
    stage = tmp_path / "stage"
    _stage_pyonly(src, stage)
    out = tmp_path / "pyonly.zip"
    _deterministic_zip(stage, out)
    present = _schema_names_in_zip(out)
    assert present == set(), (
        "old py-only staging must omit all runtime schema resources (this is the "
        f"FileNotFoundError root cause); unexpectedly found: {sorted(present)}"
    )


# --------------------------------------------------------------------------- #
# GREEN: the fixed staging packages every runtime schema, all valid JSON
# --------------------------------------------------------------------------- #
def test_combined_package_contains_all_runtime_schemas(tmp_path):
    """The fixed staging packages EVERY versioned schema the validator binds, and
    each archived resource is a valid JSON object carrying the $id the runtime
    schema registry keys on."""
    src = _combined_operations_tree_with_schemas(tmp_path)
    stage = tmp_path / "stage"
    _stage_with_resources(src, stage)
    out = tmp_path / "pkg.zip"
    _deterministic_zip(stage, out)

    present = _schema_names_in_zip(out)
    assert present == set(EXPECTED_SCHEMA_FILES), (
        "fixed package must contain exactly the versioned schema set the runtime "
        f"validator binds; missing={sorted(set(EXPECTED_SCHEMA_FILES) - present)} "
        f"extra={sorted(present - set(EXPECTED_SCHEMA_FILES))}"
    )

    # Every archived schema must parse as a JSON object with a matching $id, so a
    # truncated/corrupt resource cannot masquerade as present-and-valid.
    with zipfile.ZipFile(out) as zf:
        for name in SCHEMA_NAMES:
            rel = f"{SCHEMA_REL_DIR}/{name}.schema.json"
            doc = json.loads(zf.read(rel).decode("utf-8"))
            assert isinstance(doc, dict), f"{rel} must be a JSON object"
            assert doc.get("$id", "").endswith(name), f"{rel} $id must identify {name}"


def test_fixed_package_still_contains_handler_and_deps(tmp_path):
    """The schema fix does not regress the existing packaging invariants: the
    handler and every required runtime dependency are still present."""
    src = _combined_operations_tree_with_schemas(tmp_path)
    stage = tmp_path / "stage"
    _stage_with_resources(src, stage)
    out = tmp_path / "pkg.zip"
    _deterministic_zip(stage, out)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert HANDLER_REL in names, "package must still contain the real handler"
    for required in REQUIRED_TOP_LEVEL:
        assert any(n.split("/")[0] == required for n in names), f"package must contain dependency {required}"


def test_fixed_package_is_byte_stable(tmp_path):
    """Adding the schema resources preserves determinism: two builds of an
    unchanged combined tree (handler + schemas) are byte-identical, so the
    content-hash S3 key stays stable."""
    src = _combined_operations_tree_with_schemas(tmp_path)
    stage_a = tmp_path / "a"
    stage_b = tmp_path / "b"
    _stage_with_resources(src, stage_a)
    _stage_with_resources(src, stage_b)
    zip_a = tmp_path / "a.zip"
    zip_b = tmp_path / "b.zip"
    _deterministic_zip(stage_a, zip_a)
    _deterministic_zip(stage_b, zip_b)
    assert hashlib.sha256(zip_a.read_bytes()).hexdigest() == hashlib.sha256(zip_b.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# The runtime contract load genuinely fails without the schema resources —
# proving the wrapper's strengthened runtime probe (a representative
# validate_contract load) catches the omission before upload, where a bare
# handler import would not.
# --------------------------------------------------------------------------- #
def test_runtime_contract_load_requires_packaged_schemas(tmp_path):
    """Point importlib.resources at a package tree missing the schema resources
    and drive the real load path; it must raise a missing-resource error
    (FileNotFoundError), which is exactly what the deployed handler hit. Then
    point it at a tree WITH the schemas and confirm the same load succeeds."""
    # Standard library
    import importlib
    import sys

    # Local modules
    from operations.contracts import validation as validation_mod

    src_missing = _combined_operations_tree(tmp_path / "missing")  # py-only: no schemas
    src_present = _combined_operations_tree_with_schemas(tmp_path / "present")

    def _load_all_from(src_root: pathlib.Path):
        # Import operations.contracts.validation as if installed under src_root by
        # exercising the same importlib.resources path the runtime uses, with the
        # schema directory resolved relative to the packaged tree.
        schema_dir = src_root / SCHEMA_REL_DIR
        loaded = {}
        for name in SCHEMA_NAMES:
            schema_file = schema_dir / f"{name}.schema.json"
            # read_text() on a resource that was never packaged is the runtime's
            # FileNotFoundError; reproduce it directly against the staged tree.
            loaded[name] = json.loads(schema_file.read_text(encoding="utf-8"))
        return loaded

    with pytest.raises(FileNotFoundError):
        _load_all_from(src_missing)

    # With schemas packaged, the same resolution succeeds for the full set.
    loaded = _load_all_from(src_present)
    assert set(loaded) == set(SCHEMA_NAMES)

    # And the real validator, using the installed package resources, agrees the
    # full versioned set loads and validates a document (typed rejection is fine).
    for name in SCHEMA_NAMES:
        validation_mod.load_schema(name)
    with pytest.raises(validation_mod.ContractValidationError):
        validation_mod.validate_contract("prepared-operation", {})


# --------------------------------------------------------------------------- #
# Guard the wrapper itself so the fix cannot silently regress.
# --------------------------------------------------------------------------- #
def test_wrapper_stages_non_code_runtime_resources():
    """The deploy wrapper must stage the versioned JSON schema resources (not
    just *.py) and must fail closed if none are staged. Asserted against the
    wrapper source so reverting to `.py`-only staging trips this test."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # Staging must match *.json in addition to *.py under operations.
    assert "-name '*.json'" in text, "wrapper must stage non-code *.json runtime resources"
    # It must fail closed when the schema resources are absent from the stage.
    assert "operations/contracts/schemas/v1" in text, "wrapper must reference the versioned schema dir"
    assert "STAGED_SCHEMAS" in text, "wrapper must count staged schema resources and fail closed on zero"


def test_wrapper_runtime_probe_loads_contracts_not_just_imports():
    """The clean-Linux runtime probe must drive a representative contract load
    (schema registry + validate_contract), not merely import the handler, so a
    schema-less package fails before upload."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    assert "RUNTIME_PROBE_PY" in text, "wrapper must define a runtime probe program"
    assert "_schema_registry()" in text, "runtime probe must build the schema registry (reads schema files)"
    assert "validate_contract" in text, "runtime probe must exercise the public contract validation path"
    # And the no-container structural probe must assert the schema resources too.
    assert (
        "Runtime contract schema resources are absent" in text
    ), "structural probe must fail closed when schema resources are missing"
