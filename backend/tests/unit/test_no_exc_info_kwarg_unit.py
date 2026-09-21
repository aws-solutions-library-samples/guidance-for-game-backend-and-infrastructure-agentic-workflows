"""Lint guard preventing the #409 regression from reappearing.

The project logger is loguru, which does NOT implement the stdlib ``logging``
keyword ``exc_info=``. Passing it silently drops the traceback (see
``test_loguru_exception_logging_unit``). This guard scans the checked-in
``backend/src`` tree and fails if any ``exc_info=`` keyword reappears, so the
correct ``logger.exception`` / ``logger.opt(exception=...)`` patterns are the
only way exceptions get logged.

Implemented as an AST scan (not a text ``grep``) so string literals and comments
mentioning ``exc_info`` do not trip it — only an actual ``exc_info=`` keyword
argument in a call counts.
"""

# Standard library
import ast
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

# backend/src, resolved relative to this test file (tests/unit/ -> ../../src).
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _iter_python_files():
    yield from sorted(_SRC_ROOT.rglob("*.py"))


def _exc_info_call_lines(source: str) -> list[int]:
    """Return line numbers of any call passing an ``exc_info=`` keyword."""
    tree = ast.parse(source)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "exc_info":
                    offenders.append(node.lineno)
    return offenders


def test_backend_src_has_no_exc_info_kwarg():
    """No call in backend/src may pass ``exc_info=`` (loguru ignores it)."""
    assert _SRC_ROOT.is_dir(), f"expected backend/src at {_SRC_ROOT}"

    offenders: list[str] = []
    for path in _iter_python_files():
        source = path.read_text(encoding="utf-8")
        # Fast reject before the AST parse.
        if "exc_info" not in source:
            continue
        for lineno in _exc_info_call_lines(source):
            rel = path.relative_to(_SRC_ROOT.parent)
            offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "loguru does not implement `exc_info=`; it silently drops the traceback "
        "(#409). Use logger.exception(msg) inside an except block, or "
        "logger.opt(exception=exc) outside one. Offending call sites:\n  " + "\n  ".join(offenders)
    )
