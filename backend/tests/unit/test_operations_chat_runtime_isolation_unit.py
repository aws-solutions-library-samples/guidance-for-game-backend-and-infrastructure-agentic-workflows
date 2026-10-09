"""The chat runtime never imports the dormant observe operations modules (#413).

The read-only observation path under ``operations`` is deployed only as its own
Lambda and is dormant in the default chat runtime. This test statically scans
the chat entrypoint and every specialist agent module and asserts that none of
them imports an observe-only operations module — so the dormancy claim stays
true as a tested invariant rather than a comment.
"""

from __future__ import annotations

# Standard library
import ast
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src"

# Operations modules that belong only to the observe Lambda and must never be
# imported by the chat runtime or its specialists.
_FORBIDDEN_PREFIXES = (
    "operations.observation",
    "operations.observation_store",
    "operations.observation_handler",
    "operations.observe",
    "operations.claims",
    "operations.settings",
)


def _chat_runtime_files() -> list[Path]:
    files = [_SRC / "agentcore_main.py"]
    agents_dir = _SRC / "agents"
    if agents_dir.is_dir():
        files.extend(sorted(agents_dir.rglob("*.py")))
    return [path for path in files if path.is_file()]


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_chat_runtime_files_exist() -> None:
    files = _chat_runtime_files()
    assert any(path.name == "agentcore_main.py" for path in files), "chat entrypoint not found"


def test_chat_runtime_never_imports_observe_operations() -> None:
    offenders: list[str] = []
    for path in _chat_runtime_files():
        modules = _imported_modules(path.read_text(encoding="utf-8"))
        for module in modules:
            if module.startswith(_FORBIDDEN_PREFIXES):
                offenders.append(f"{path.relative_to(_SRC)} imports {module}")
    assert not offenders, "chat runtime imports dormant observe modules:\n" + "\n".join(offenders)
