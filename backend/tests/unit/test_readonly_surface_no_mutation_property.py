#!/usr/bin/env python3
"""Property-based test: the chat runtime exposes only read operations and ships no mutation code.

# Feature: source-control-connector-readonly-split, Property 1: the chat runtime exposes only read operations and ships no mutation code

This is a non-optional MR security-posture property. It introspects the shipped provider
interface (:class:`~connector.provider.SourceControlReader`), the connector-exported tool
surface (:mod:`connector.tools`), and the public service entry points
(:mod:`connector.service`), asserting each is a read operation — and that **no**
provider-mutation symbol (``create_branch``, ``commit_files``, ``open_change_proposal``,
``propose_change``, ``propose_infrastructure_change``, or a ``SourceControlWriter``
interface) exists anywhere in the shipped read-only ``connector`` package, whether active or
inactive.

"Anywhere, active or inactive" is checked structurally: every module in the ``connector``
package is imported and probed for a mutation attribute, and every ``connector`` source file
is parsed with the ``ast`` module and probed for a mutation *definition* (function, async
function, class, or module-level binding). AST parsing deliberately ignores comments and
docstrings, so the design's prose describing the *removed* write path (which legitimately
names those symbols) does not produce a false positive — only real, defined code counts.

Validates: Requirements 1.1, 1.2, 4.1, 4.2, 4.3, 5.4, 12.1, 12.3
"""

# Standard library
import ast
import importlib
import pkgutil
from pathlib import Path

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
import connector
from connector.provider import SourceControlReader

pytestmark = pytest.mark.unit

# The provider-mutation symbols that must not exist anywhere in the shipped read-only
# package (the write path moved to the #314 executor and is preserved only in branch
# history).
_MUTATION_SYMBOLS = (
    "create_branch",
    "commit_files",
    "open_change_proposal",
    "propose_change",
    "propose_infrastructure_change",
    "SourceControlWriter",
)

# The complete read operation set the shipped provider interface may expose.
_READ_OPS = frozenset({"get_file", "get_files"})


def _connector_module_names() -> list[str]:
    """Return every importable module name in the shipped ``connector`` package."""
    return [name for _, name, _ in pkgutil.iter_modules(connector.__path__, prefix="connector.")]


def _connector_source_files() -> list[Path]:
    """Return every ``.py`` source file in the shipped ``connector`` package directory."""
    package_dir = Path(connector.__file__).parent
    return sorted(package_dir.glob("*.py"))


def _defined_names(source_path: Path) -> set[str]:
    """Collect every name DEFINED in ``source_path`` via AST (ignores comments/docstrings).

    Captures function/async-function/class definitions and module-level (and nested)
    name bindings, so an inactive/unwired definition is still caught while prose mentions
    in docstrings and comments are not.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_read_interface_and_public_surface_are_read_only():
    """The provider interface, exported tool, and public service entry points are read-only.

    A one-shot structural check of the positive surface (Req 1.1, 1.2, 4.1, 4.2):

    - ``SourceControlReader`` declares exactly the read operation set and no mutation method.
    - ``connector.tools`` exports the read tool ``get_iac_file`` and no write tool.
    - ``connector.service.__all__`` is exactly the single read entry point ``read_iac_files``.
    """
    # Provider interface: exactly the read operation set, no mutation method.
    assert set(SourceControlReader.__abstractmethods__) == _READ_OPS
    for symbol in _MUTATION_SYMBOLS:
        assert not hasattr(SourceControlReader, symbol)

    # Connector-exported tool surface: the read tool is present, the write tool is gone.
    tools = importlib.import_module("connector.tools")
    assert hasattr(tools, "get_iac_file")
    assert not hasattr(tools, "propose_infrastructure_change")

    # Public service entry points: exactly the single read entry point.
    service = importlib.import_module("connector.service")
    assert list(service.__all__) == ["read_iac_files"]


# Feature: source-control-connector-readonly-split, Property 1: the chat runtime exposes only read operations and ships no mutation code
@settings(max_examples=100)
@given(symbol=st.sampled_from(_MUTATION_SYMBOLS))
def test_property1_no_mutation_symbol_anywhere_in_package(symbol):
    """No mutation symbol is importable, callable, or defined anywhere in the package.

    For every generated mutation symbol, assert it is neither an attribute of any imported
    ``connector`` module (active/reachable) nor a definition in any ``connector`` source
    file (inactive/unwired) — Req 4.1, 4.2, 4.3, 5.4, 12.1, 12.3.
    """
    # (a) Not importable/attribute-reachable on any shipped connector module.
    for module_name in _connector_module_names():
        module = importlib.import_module(module_name)
        assert not hasattr(module, symbol), f"{module_name} exposes mutation symbol {symbol!r}"

    # (b) Not DEFINED (function/class/binding) in any connector source file, active or not.
    for source_path in _connector_source_files():
        assert symbol not in _defined_names(source_path), f"{source_path.name} defines mutation symbol {symbol!r}"
