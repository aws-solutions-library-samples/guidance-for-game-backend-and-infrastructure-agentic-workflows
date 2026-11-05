"""Static guard: backend code creates no per-instance values at import time.

On AgentCore Runtime platform version V2 every instance is restored from one
snapshot taken after startup, so anything computed while the entrypoint is
imported is identical in every instance: random values, keys, identifiers,
timestamps, monotonic references, the host name, the PID, and cached JWKS. Such
values must be created while handling a request (see
``utils/runtime_instance.py``). This test parses ``backend/src`` and fails on

* a call that runs at import (module and class bodies, decorators, default
  argument values, and module-level ``if``/``try``/``with``/loop blocks) to an
  entropy, clock, host, process, or JWKS source; and
* any call to the ``random`` module's shared generator (``random.uniform``,
  ``random.random``, and so on) anywhere, because a V2 snapshot shares its state
  across instances. ``random.SystemRandom`` (os.urandom per call) is allowed, and
  only the per-instance initialization may reseed the shared generator.

It does not follow calls into functions; ``prewarm_container`` and the other
startup functions are reviewed by hand and documented in the module docstrings.
"""

# Standard library
import ast
import pathlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

SRC_ROOT = pathlib.Path(__file__).parents[2] / "src"

IMPORT_TIME_DENYLIST = frozenset(
    {
        "os.urandom",
        "os.getpid",
        "socket.gethostname",
        "platform.node",
        "uuid.uuid1",
        "uuid.uuid4",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "jwt.PyJWKClient",
        "runtime_identity._jwk_client",
    }
)
IMPORT_TIME_DENIED_PREFIXES = ("secrets.", "random.")
ALLOWED_RANDOM_CALLS = frozenset({"random.SystemRandom"})
# The per-instance initialization reseeds the shared generator after restore.
RESEED_ALLOWED_IN = frozenset({"utils/runtime_instance.py"})


def _source_files(root: pathlib.Path):
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _aliases(tree: ast.Module, module_name: str) -> dict[str, str]:
    """Map local names to the dotted names they were imported as."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # Module-local helpers (for example ``_jwk_client``) resolve to the module.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            aliases.setdefault(node.name, f"{module_name}.{node.name}")
    return aliases


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _resolve(call: ast.Call, aliases: dict[str, str]) -> str | None:
    dotted = _dotted(call.func)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    resolved_head = aliases.get(head, head)
    return f"{resolved_head}.{rest}" if rest else resolved_head


def _import_time_nodes(body):
    """Yield the AST nodes whose expressions run when the module is imported."""
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from statement.decorator_list
            yield from statement.args.defaults
            yield from (default for default in statement.args.kw_defaults if default is not None)
            if statement.returns is not None:
                yield statement.returns
        elif isinstance(statement, ast.ClassDef):
            yield from statement.decorator_list
            yield from statement.bases
            yield from _import_time_nodes(statement.body)
        elif isinstance(statement, (ast.If, ast.While)):
            yield statement.test
            yield from _import_time_nodes(statement.body)
            yield from _import_time_nodes(statement.orelse)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            yield statement.iter
            yield from _import_time_nodes(statement.body)
            yield from _import_time_nodes(statement.orelse)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            yield from (item.context_expr for item in statement.items)
            yield from _import_time_nodes(statement.body)
        elif isinstance(statement, ast.Try):
            yield from _import_time_nodes(statement.body)
            for handler in statement.handlers:
                yield from _import_time_nodes(handler.body)
            yield from _import_time_nodes(statement.orelse)
            yield from _import_time_nodes(statement.finalbody)
        else:
            yield statement


def _calls_outside_functions(node: ast.AST):
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(current, ast.Call):
            yield current
        stack.extend(ast.iter_child_nodes(current))


def _module_name(path: pathlib.Path, root: pathlib.Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _import_time_violations(root: pathlib.Path = SRC_ROOT) -> list[str]:
    violations = []
    for path in _source_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _aliases(tree, _module_name(path, root))
        for node in _import_time_nodes(tree.body):
            for call in _calls_outside_functions(node):
                name = _resolve(call, aliases)
                if name is None or name in ALLOWED_RANDOM_CALLS:
                    continue
                if name in IMPORT_TIME_DENYLIST or name.startswith(IMPORT_TIME_DENIED_PREFIXES):
                    violations.append(f"{path.relative_to(root)}:{call.lineno}: {name}()")
    return violations


def _shared_random_violations(root: pathlib.Path = SRC_ROOT) -> list[str]:
    violations = []
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _aliases(tree, _module_name(path, root))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = _resolve(call, aliases)
            if name is None or not name.startswith("random.") or name in ALLOWED_RANDOM_CALLS:
                continue
            if name == "random.seed" and relative in RESEED_ALLOWED_IN:
                continue
            violations.append(f"{relative}:{call.lineno}: {name}()")
    return violations


def test_no_per_instance_value_is_created_at_import():
    assert _import_time_violations() == []


def test_backend_code_never_uses_the_shared_random_generator():
    assert _shared_random_violations() == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nKEY = os.urandom(32)\n", ["os.urandom()"]),
        ("import time as _t\nSTART = _t.monotonic()\n", ["time.monotonic()"]),
        ("from datetime import datetime\nif True:\n    NOW = datetime.now()\n", ["datetime.datetime.now()"]),
        ("import uuid\nclass C:\n    ID = uuid.uuid4()\n", ["uuid.uuid4()"]),
        ("import secrets\ndef f(token=secrets.token_hex(8)):\n    return token\n", ["secrets.token_hex()"]),
        ("import random\nJITTER = random.SystemRandom()\n", []),
        ("import os\ndef f():\n    return os.urandom(32)\n", []),
    ],
)
def test_the_scanner_finds_import_time_calls(tmp_path, source, expected):
    # Pin the scanner itself so the guard cannot silently pass.
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")

    found = [violation.split(": ", 1)[1] for violation in _import_time_violations(tmp_path)]

    assert found == expected


def test_the_scanner_finds_shared_random_calls_anywhere(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import random\nfrom random import uniform\n"
        "def f():\n    return random.random() + uniform(0, 1) + random.SystemRandom().random()\n",
        encoding="utf-8",
    )

    found = [violation.split(": ", 1)[1] for violation in _shared_random_violations(tmp_path)]

    assert sorted(found) == ["random.random()", "random.uniform()"]
