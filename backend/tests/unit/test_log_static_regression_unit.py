"""Static regression guard for high-risk logging call sites (#472).

An AST scan over the known prompt/identity/session logging call sites fails if a
future change passes a forbidden identity or prompt expression into a logger
call. This catches reintroduction of raw-identifier logging that a runtime test
might miss because the specific branch was not exercised.

The guard is intentionally conservative: it inspects f-string and ``%``/``+``
formatting arguments of ``logger.<level>(...)`` calls in the high-risk modules
and rejects any that interpolate a bare forbidden identifier name (for example
``actor_id``, ``session_id``, ``thread_id``, ``user_prompt``, ``display_name``).
Values that are first passed through an approved sink-safe helper
(``redact_identifier``, ``normalize_log_value``, ``sanitize_log_data``) or that
only report a length/count are allowed.
"""

# Standard library
import ast
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src"

# Modules audited for raw prompt/identity/session logging.
HIGH_RISK_MODULES = [
    _SRC / "agentcore_main.py",
    _SRC / "utils" / "semantic_memory.py",
    _SRC / "agents" / "orchestrator.py",
]

# Bare names that must not be interpolated raw into a log message.
FORBIDDEN_NAMES = {
    "actor_id",
    "session_id",
    "thread_id",
    "persistent_user_id",
    "user_prompt",
    "user_message",
    "display_name",
    "username",
    "email",
    "memory_content",
    "content",
    "name",
}

# Helper calls whose return value is safe to log.
SAFE_WRAPPERS = {"redact_identifier", "normalize_log_value", "sanitize_log_data", "len"}

LOG_LEVELS = {"info", "warning", "error", "debug", "exception", "critical", "trace"}


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in LOG_LEVELS
        and isinstance(func.value, ast.Name)
        and func.value.id == "logger"
    )


def _safe_wrapped(node: ast.AST) -> bool:
    """True if the expression is wrapped in an approved sink-safe helper."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SAFE_WRAPPERS


def _forbidden_bare_names(expr: ast.AST) -> set[str]:
    """Return forbidden bare Name ids referenced directly (not inside a safe
    wrapper call) within a formatted-value expression."""
    found: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, call: ast.Call) -> None:
            if _safe_wrapped(call):
                return  # Do not descend into redact_identifier(...) etc.
            self.generic_visit(call)

        def visit_Name(self, name: ast.Name) -> None:
            if name.id in FORBIDDEN_NAMES:
                found.add(name.id)

    _Visitor().visit(expr)
    return found


def _collect_log_message_expressions(call: ast.Call):
    """Yield the formatted sub-expressions that compose the log message.

    Covers f-strings (JoinedStr formatted values), ``%`` formatting, and ``+``
    string concatenation in the first positional argument.
    """
    if not call.args:
        return
    msg = call.args[0]
    stack = [msg]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    yield value.value
        elif isinstance(node, ast.BinOp):
            stack.append(node.left)
            stack.append(node.right)


def test_high_risk_modules_do_not_log_raw_identity_or_prompt():
    violations = []
    for module in HIGH_RISK_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_logger_call(node)):
                continue
            for expr in _collect_log_message_expressions(node):
                if _safe_wrapped(expr):
                    continue
                bad = _forbidden_bare_names(expr)
                if bad:
                    violations.append(f"{module.name}:{node.lineno} logs raw {sorted(bad)}")

    assert not violations, "Raw prompt/identity/session logging detected:\n" + "\n".join(violations)
