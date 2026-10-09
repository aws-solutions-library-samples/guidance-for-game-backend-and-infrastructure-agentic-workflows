"""Static regression guard for high-risk logging call sites (#472).

An AST scan over every module in ``backend/src`` fails if a logging call passes a
forbidden identity or prompt expression into the sink. This catches
reintroduction of raw-identifier logging that a runtime test might miss because
the specific branch was not exercised.

The guard inspects every argument of a logger call — positional and keyword,
and across chained ``logger.opt(...)`` / ``logger.bind(...)`` receivers — and
flags an argument that references a forbidden identity/prompt value, whether it
is interpolated through an f-string, ``%`` / ``+`` / ``.format()`` formatting,
or passed as a Loguru brace-style argument. The logger receiver may be the
``logger`` name or the ``_get_logger()`` lazy accessor. The message argument of
``log_sanitized_exception(log, message, exc)`` is treated as a sink as well. A
forbidden value is detected as a bare name (``actor_id``), an attribute access
whose final name is forbidden (``context.session_id``), or a mapping lookup for
a forbidden key (``user_context.get("email")`` / ``payload["prompt"]``).

``name`` and ``content`` are forbidden only in ``semantic_memory.py`` (the
extracted user name and memory text); elsewhere they are ordinary locals.

Only two escapes are allowed: a value first wrapped in an approved
non-reversible/bounding helper (``redact_identifier``, ``len``) or a reference
to a forbidden *key name as a string literal* (which is data, not the value).
A presence/identity check (``x is not None``), a membership test
(``'prompt' in data``), and a boolean coercion (``bool(...)``) reveal only
whether a value exists, not the value, so they are not flagged; reading a plain
attribute off a bare name (``prompt.arn``) discloses that attribute, not the
base object. ``sanitize_log_data`` and ``normalize_log_value`` are **not**
treated as safe wrappers here: they keep the value's content (merely redacting
known patterns or stripping control characters), so logging a prompt or raw
identifier through them still discloses it.

The module ends with self-tests that run known bypass snippets through the
scanner so the guard itself cannot silently stop catching them.
"""

# Standard library
import ast
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src"

# Bare names / attribute tails / mapping keys that must not reach the sink raw.
FORBIDDEN_NAMES = {
    "actor_id",
    "session_id",
    "thread_id",
    "isolated_thread_id",
    "persistent_user_id",
    "subject_id",
    "user_prompt",
    "user_message",
    "prompt",
    "query",
    "display_name",
    "username",
    "email",
    "memory_content",
    "user_info",
    "user_context",
    "user_id",
    "claims",
}

# Names that are forbidden only in modules that actually carry that value. ``name``
# and ``content`` are far too common as ordinary locals (a resource name, HTTP
# content) to flag everywhere, but in semantic memory they are the extracted
# user name and the memory text, which must never be logged.
MODULE_SCOPED_FORBIDDEN = {
    "semantic_memory.py": {"name", "content"},
}

# Helpers whose return value is safe to log: a bounded, non-reversible token, a
# pure length/count, or the runtime type. Deliberately excludes
# sanitize_log_data / normalize_log_value, which preserve the underlying content.
SAFE_WRAPPERS = {"redact_identifier", "len", "type"}

# Methods that return structural metadata (key names, counts) rather than a
# forbidden value, so a forbidden receiver passed through them is safe to log.
SAFE_METHODS = {"keys", "values", "items"}

LOG_LEVELS = {"info", "warning", "error", "debug", "exception", "critical", "trace", "success"}

# Chained logger builders whose result is still a logger (so the terminal
# ``.<level>(...)`` is still a logger call we must inspect).
LOGGER_BUILDERS = {"opt", "bind", "contextualize", "patch", "level"}


def _receiver_is_logger(func: ast.expr) -> bool:
    """Resolve ``logger``, ``logger.opt(...).bind(...)`` etc. back to a logger.

    Returns True if the attribute chain's ultimate receiver is a logger — either
    the ``logger`` name or a ``_get_logger()`` call (the lazy accessor
    ``security.py`` uses) — reached only through known logger-builder calls.
    """
    node: ast.AST = func
    # Walk down through builder calls: logger.opt(...).bind(...).error
    while isinstance(node, ast.Attribute):
        node = node.value
        # A builder call in the chain: unwrap the call back to its own func.
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOGGER_BUILDERS:
            node = node.func.value
    if isinstance(node, ast.Name) and node.id == "logger":
        return True
    # ``_get_logger().warning(...)`` — the receiver is the accessor call.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_get_logger":
        return True
    return False


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in LOG_LEVELS and _receiver_is_logger(func)


# The sanitized-exception helper is itself a sink: its second positional
# argument is the message that reaches the logger, so a forbidden value
# interpolated there would be logged.
_SANITIZED_HELPER = "log_sanitized_exception"


def _is_sanitized_helper_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id == _SANITIZED_HELPER:
        return True
    if isinstance(func, ast.Attribute) and func.attr == _SANITIZED_HELPER:
        return True
    return False


def _safe_wrapped(node: ast.AST) -> bool:
    """True if the expression is a safe accessor over its argument/receiver.

    Safe forms: ``redact_identifier(x)`` / ``len(x)`` / ``type(x)`` (bounded or
    non-content), and ``x.keys()`` / ``x.values()`` / ``x.items()`` (structural
    metadata, not the forbidden value itself).
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in SAFE_WRAPPERS
    if isinstance(func, ast.Attribute):
        return func.attr in SAFE_WRAPPERS or func.attr in SAFE_METHODS
    return False


def _forbidden_in_expression(expr: ast.AST, forbidden: set[str]) -> set[str]:
    """Return forbidden identity/prompt references inside a logged expression.

    Flags:
      * bare ``Name`` nodes whose id is forbidden;
      * ``Attribute`` nodes whose final ``attr`` is forbidden (``ctx.session_id``);
      * ``Subscript`` with a constant forbidden key (``data["prompt"]``);
      * ``.get("<forbidden>")`` lookups.

    Does not descend into an approved safe wrapper. A forbidden *key name* that
    appears only as a string literal argument is data, not the value, and is not
    flagged on its own. A presence/identity check (``x is not None``,
    ``'prompt' in data``) and a boolean coercion (``bool(data.get('prompt'))``)
    disclose only whether a value exists, not the value, so they are skipped.
    """
    found: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, call: ast.Call) -> None:
            if _safe_wrapped(call):
                return  # redact_identifier(...)/len(...) output is safe.
            # ``bool(...)`` yields only existence, not the underlying value.
            if isinstance(call.func, ast.Name) and call.func.id == "bool":
                return
            # Detect dict.get("<forbidden>") -> logs the forbidden value.
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr == "get" and call.args:
                key = call.args[0]
                if isinstance(key, ast.Constant) and key.value in forbidden:
                    found.add(str(key.value))
            self.generic_visit(call)

        def visit_Compare(self, node: ast.Compare) -> None:
            # A comparison (``x is not None``, ``x == 'default'``) reveals only
            # the comparison outcome, not the value. Skip the whole node.
            return

        def visit_Name(self, name: ast.Name) -> None:
            if name.id in forbidden:
                found.add(name.id)

        def visit_Attribute(self, attr: ast.Attribute) -> None:
            if attr.attr in forbidden:
                found.add(attr.attr)
            # Reading an attribute off a bare name discloses that attribute, not
            # the base object, so ``prompt.arn`` is safe while ``ctx.session_id``
            # is caught by the tail check above. Only descend when the base is
            # itself a compound expression (a nested attribute, subscript, or
            # call) that could independently carry a forbidden value.
            if not isinstance(attr.value, ast.Name):
                self.visit(attr.value)

        def visit_Subscript(self, sub: ast.Subscript) -> None:
            key = sub.slice
            if isinstance(key, ast.Constant) and key.value in forbidden:
                found.add(str(key.value))
            self.generic_visit(sub)

    _Visitor().visit(expr)
    return found


def _logged_expressions(call: ast.Call, *, sanitized_helper: bool = False):
    """Yield every sub-expression that could carry a forbidden value into the sink.

    Covers: f-strings (JoinedStr formatted values); ``%`` and ``+`` operands;
    ``"...".format(...)`` arguments; and every positional/keyword argument
    (Loguru brace-style ``logger.info("{}", actor_id)`` and
    ``logger.bind(x=actor_id)``). The format-string literal itself is skipped;
    its substituted values are what we inspect.

    For a ``log_sanitized_exception(log, message, exc, ...)`` call the sink is the
    second positional argument (the message). The first positional is the logger
    and the third is the exception object (deliberately not rendered), so only
    the message and any keyword message pieces are inspected.
    """
    nodes: list[ast.AST] = []

    def _expand(node: ast.AST) -> None:
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    nodes.append(value.value)
        elif isinstance(node, ast.BinOp):
            _expand(node.left)
            _expand(node.right)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            # "...{}".format(value) — inspect the substituted values only.
            for arg in node.args:
                nodes.append(arg)
            for kw in node.keywords:
                nodes.append(kw.value)
        elif isinstance(node, ast.Constant):
            return  # a bare literal carries nothing
        else:
            nodes.append(node)

    if sanitized_helper:
        # Inspect only the message argument (second positional).
        if len(call.args) >= 2:
            _expand(call.args[1])
        yield from nodes
        return

    # First positional arg is the message/format; remaining positional and all
    # keyword args are Loguru brace substitutions / bound extras.
    for arg in call.args:
        _expand(arg)
    for kw in call.keywords:
        _expand(kw.value)

    # Builder calls in the receiver chain (logger.bind(x=actor_id).info(...))
    # can themselves carry a forbidden value into the record's extras.
    node: ast.AST = call.func
    while isinstance(node, ast.Attribute):
        node = node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOGGER_BUILDERS:
            for arg in node.args:
                _expand(arg)
            for kw in node.keywords:
                _expand(kw.value)
            node = node.func.value

    yield from nodes


def _scan_source(source: str, filename: str = "<test>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    # ``name`` and ``content`` are forbidden only in the module that carries the
    # extracted user name and memory text.
    forbidden = set(FORBIDDEN_NAMES) | MODULE_SCOPED_FORBIDDEN.get(filename, set())
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_logger_call(node):
            exprs = _logged_expressions(node)
        elif _is_sanitized_helper_call(node):
            exprs = _logged_expressions(node, sanitized_helper=True)
        else:
            continue
        for expr in exprs:
            bad = _forbidden_in_expression(expr, forbidden)
            if bad:
                violations.append(f"{filename}:{getattr(node, 'lineno', '?')} logs raw {sorted(bad)}")
    return violations


def _python_sources():
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_backend_src_does_not_log_raw_identity_or_prompt():
    violations: list[str] = []
    for module in _python_sources():
        violations.extend(_scan_source(module.read_text(encoding="utf-8"), module.name))
    assert not violations, "Raw prompt/identity/session logging detected:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Self-tests: the scanner must catch each known bypass shape and must not flag a
# safe shape. These keep the guard from silently regressing.
# ---------------------------------------------------------------------------

_BYPASS_SNIPPETS = [
    'logger.info(f"...{sanitize_log_data(user_prompt, 100)}")',
    'logger.info(f"...{normalize_log_value(actor_id)}")',
    'logger.info("Actor: {}", actor_id)',
    '"Actor: %s" % actor_id and logger.info("Actor: %s" % actor_id)',
    'logger.info("Actor: " + actor_id)',
    "logger.info(actor_id)",
    'logger.info("...".format(actor_id))',
    'logger.info(f"...{context.session_id}")',
    "logger.info(f\"...{user_context.get('email')}\")",
    "logger.info(f\"...{data['prompt']}\")",
    'logger.info(f"...{query}")',
    'logger.opt(exception=e).error(f"...{actor_id}")',
    "logger.bind(x=actor_id).info('x')",
    'logger.info(f"...{user_prompt[:50]}")',
    # Newly covered receivers, names, and sinks.
    "logger.info(f\"...{user_context.get('user_id')}\")",
    "logger.info(f\"...{claims['sub']}\" )",
    'logger.info(f"...{user_id}")',
    'logger.info(f"...{user_context}")',
    '_get_logger().warning(f"...{actor_id}")',
    'log_sanitized_exception(logger, f"... {actor_id}", e)',
]

# Snippets that must flag only when scanned as the module that owns the value.
_MODULE_SCOPED_SNIPPETS = [
    ('logger.info(f"name: {name}")', "semantic_memory.py"),
    ('logger.info(f"content: {content[:50]}")', "semantic_memory.py"),
]

_SAFE_SNIPPETS = [
    'logger.info(f"...{redact_identifier(actor_id)}")',
    'logger.info(f"...{len(user_prompt)}")',
    'logger.info("prompt length: {}", len(user_prompt))',
    'logger.bind(request_id=redact_identifier(actor_id)).info("x")',
    'logger.info("a constant message")',
    'logger.info(f"...{response_text}")',
    # Presence/identity checks and boolean coercions reveal existence, not value.
    "logger.info(f\"...{bool(data.get('prompt'))}\")",
    'logger.info(f"...{query is not None}")',
    'logger.info(f"...{self.session_id is not None}")',
    'logger.info(f"...{prompt.arn}")',
    # ``name`` / ``content`` are ordinary locals outside semantic_memory.py.
    'logger.info(f"resource name: {name}")',
    'logger.info(f"http content: {content}")',
    # The exception object passed to the helper is not a rendered value.
    'log_sanitized_exception(logger, "a constant message", e)',
]


@pytest.mark.parametrize("snippet", _BYPASS_SNIPPETS)
def test_scanner_catches_known_bypass(snippet: str):
    assert _scan_source(snippet), f"guard failed to flag a known leak: {snippet}"


@pytest.mark.parametrize("snippet,filename", _MODULE_SCOPED_SNIPPETS)
def test_scanner_catches_module_scoped_leak(snippet: str, filename: str):
    # Flagged in the owning module...
    assert _scan_source(snippet, filename), f"guard failed to flag a module-scoped leak: {snippet}"
    # ...but not in an unrelated module, where ``name``/``content`` are common.
    assert not _scan_source(snippet, "some_other_module.py"), f"guard over-flagged outside its module: {snippet}"


@pytest.mark.parametrize("snippet", _SAFE_SNIPPETS)
def test_scanner_allows_safe_logging(snippet: str):
    assert not _scan_source(snippet), f"guard wrongly flagged a safe statement: {snippet}"
