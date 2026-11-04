"""Every Strands agent runs without the default stdout printer.

When ``callback_handler`` is omitted, Strands installs ``PrintingCallbackHandler``,
which writes streamed model text and tool names to stdout. The hosted AgentCore
runtime ships stdout to its CloudWatch log group, so model replies (which can
echo user-supplied names or other prompt content) would bypass the sanitized
application logger. Each ``Agent(...)`` construction must pass
``callback_handler=None``.
"""

# Standard library
import ast
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src"


def _agent_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == "Agent":
                calls.append(node)
    return calls


def _silences_stdout(call: ast.Call) -> bool:
    return any(
        keyword.arg == "callback_handler" and isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        for keyword in call.keywords
    )


def _offenders(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    return [f"{filename}:{call.lineno}" for call in _agent_calls(tree) if not _silences_stdout(call)]


def test_every_agent_construction_disables_the_stdout_printer():
    offenders = []
    constructions = 0
    for path in sorted(_SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        constructions += len(_agent_calls(tree))
        offenders.extend(_offenders(source, str(path.relative_to(_SRC))))
    assert constructions >= 3, "expected the orchestrator and specialist agent constructions"
    assert offenders == [], f"Agent(...) without callback_handler=None prints model text to stdout: {offenders}"


@pytest.mark.parametrize(
    "snippet",
    [
        "Agent(model=m, tools=t)",
        "strands.Agent(model=m)",
        "Agent(model=m, callback_handler=PrintingCallbackHandler())",
    ],
)
def test_scanner_flags_an_agent_that_keeps_the_default_printer(snippet):
    assert _offenders(snippet, "snippet.py") == ["snippet.py:1"]


def test_scanner_accepts_an_agent_with_the_printer_disabled():
    assert _offenders("Agent(model=m, callback_handler=None)", "snippet.py") == []
