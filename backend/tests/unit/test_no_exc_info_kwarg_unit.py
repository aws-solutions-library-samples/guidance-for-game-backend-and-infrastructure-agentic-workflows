"""Lint guard preventing the #409 regression from reappearing.

The project logger is loguru, which does NOT implement the stdlib ``logging``
keyword ``exc_info=``. Passing it silently drops the traceback (see
``test_loguru_exception_logging_unit``). This guard scans the checked-in
``backend/src`` tree and fails if any ``exc_info=`` keyword reappears, so the
correct ``logger.exception`` / ``logger.opt(exception=...)`` patterns are the
only way exceptions get logged.

Implemented as an AST scan (not a text ``grep``) so string literals and comments
mentioning ``exc_info`` do not trip it — only an actual ``exc_info=`` keyword
argument in a call counts. The scan also catches the statically visible
dict-unpack spelling ``**{"exc_info": True}`` (a keyword with ``arg is None``
whose value is a literal ``dict`` carrying an ``exc_info`` key), which reaches a
loguru call with the same broken effect. A dynamic unpack such as ``**kwargs``
or ``**build_kwargs()`` is intentionally NOT flagged: its contents cannot be
resolved statically, so the guard does not pretend to know what it contains.
"""

# Standard library
import ast
import textwrap
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

# backend/src, resolved relative to this test file (tests/unit/ -> ../../src).
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _keyword_passes_exc_info(keyword: ast.keyword) -> bool:
    """Return True if a call keyword passes ``exc_info`` in a statically visible form.

    Two forms are recognised:

    * A direct keyword argument ``exc_info=...`` (``keyword.arg == "exc_info"``).
    * A literal dict unpack ``**{"exc_info": ...}`` (``keyword.arg is None`` and
      ``keyword.value`` is an ``ast.Dict`` with a constant string key
      ``"exc_info"``).

    A ``**name`` / ``**call()`` unpack (value is not a literal ``ast.Dict``) is
    deliberately treated as unknown and returns False — the guard does not
    resolve dynamic keyword bags.
    """
    if keyword.arg == "exc_info":
        return True
    if keyword.arg is None and isinstance(keyword.value, ast.Dict):
        for key in keyword.value.keys:
            if isinstance(key, ast.Constant) and key.value == "exc_info":
                return True
    return False


def _iter_python_files():
    yield from sorted(_SRC_ROOT.rglob("*.py"))


def _exc_info_call_lines(source: str) -> list[int]:
    """Return line numbers of any call passing an ``exc_info=`` keyword.

    Covers both the direct ``exc_info=`` form and the statically visible literal
    dict-unpack form ``**{"exc_info": ...}``.
    """
    tree = ast.parse(source)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if _keyword_passes_exc_info(keyword):
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


# --- Unit coverage for the detector itself (red-green for finding #2) ---------
#
# These pin the detector's behaviour directly, independent of the checked-in
# tree: the literal dict-unpack form MUST be caught, and dynamic unpacks MUST
# NOT be (the guard cannot resolve them and must not pretend to).


def _detect(src: str) -> list[int]:
    return _exc_info_call_lines(textwrap.dedent(src))


def test_detector_flags_direct_exc_info_keyword():
    assert _detect('logger.error("boom", exc_info=True)') == [1]


def test_detector_flags_literal_dict_unpack():
    """``**{"exc_info": True}`` is statically visible and must be flagged."""
    assert _detect('logger.error("boom", **{"exc_info": True})') == [1]


def test_detector_flags_literal_dict_unpack_among_other_keys():
    assert _detect('logger.warning("boom", **{"depth": 1, "exc_info": True})') == [1]


def test_detector_ignores_dynamic_name_unpack():
    """``**kwargs`` cannot be resolved statically; do not pretend it can."""
    assert _detect("logger.error('boom', **kwargs)") == []


def test_detector_ignores_dynamic_call_unpack():
    """``**build_kwargs()`` is opaque to a static scan; must not be flagged."""
    assert _detect("logger.error('boom', **build_kwargs())") == []


def test_detector_ignores_exc_info_string_literal_and_comment():
    """A string/comment mentioning exc_info is not a keyword and must not trip."""
    src = """
    x = "exc_info=True is wrong for loguru"  # exc_info here is prose
    logger.exception(x)
    """
    assert _detect(src) == []


def test_detector_ignores_exc_info_dict_not_unpacked():
    """A literal dict merely assigned (not ``**`` unpacked into a call) is data."""
    src = """
    payload = {"exc_info": True}
    logger.bind(**{"request_id": "r"}).info(payload)
    """
    assert _detect(src) == []
