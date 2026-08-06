#!/usr/bin/env python3
"""Example test: agent-facing tool signatures reference only agnostic/primitive types.

The Connector's public surface toward the Orchestrator/LLM is exactly the two ``@tool``
functions in :mod:`connector.tools` (``get_iac_file`` and
``propose_infrastructure_change``). Req 9.1 requires this surface to be **provider-
agnostic**: no tool name, parameter name, parameter type annotation, or return type
annotation may reference a provider-specific type (e.g. ``GitHubProvider``), a transport
type (e.g. ``httpx.*``), or a provider name embedded in a name (``github``/``gitlab``/...).
Only primitive / JSON-serialisable / provider-agnostic types are permitted
(``str``, ``int``, ``bool``, ``float``, ``list``, ``dict``, ``None`` and their generic
parametrisations such as ``list[str]`` / ``list[dict]``).

This is an example (non-property) structural guarantee. Because the strands ``@tool``
decorator wraps the original callable in a ``DecoratedFunctionTool``, the test recovers the
underlying function via ``__wrapped__`` (falling back to ``_tool_func``) and introspects its
signature — both the raw (stringised, due to ``from __future__ import annotations``)
annotations and the resolved runtime types.

Validates: Requirements 9.1
"""

# Standard library
import inspect
import types
import typing

# Third-party packages
import pytest

# Local modules
from connector import tools as connector_tools
from connector.tools import get_iac_file, propose_infrastructure_change

pytestmark = pytest.mark.unit


# The complete set of agent-facing tools exposed by the connector (design.md).
TOOLS = (get_iac_file, propose_infrastructure_change)

# Provider / transport specific tokens that must never appear in a tool name, a parameter
# name, or any annotation string. Kept lowercase; matched case-insensitively as substrings.
FORBIDDEN_TOKENS = (
    "github",
    "gitlab",
    "bitbucket",
    "gitea",
    "codecommit",
    "httpx",
    "requests",
    "provider",  # e.g. GitHubProvider / SourceControlProvider leaking into the surface
    "pygithub",
    "octokit",
)

# The only resolved runtime types permitted anywhere in a tool signature. Generic aliases
# (list[str], list[dict], ...) are reduced to their origin (list/dict) before checking.
ALLOWED_TYPES = frozenset({str, int, bool, float, list, dict, type(None)})


def _underlying_function(tool):
    """Recover the original callable wrapped by the strands ``@tool`` decorator."""
    fn = getattr(tool, "__wrapped__", None) or getattr(tool, "_tool_func", None)
    assert fn is not None, f"could not recover underlying function for tool {tool!r}"
    assert callable(fn), f"recovered underlying object for {tool!r} is not callable"
    return fn


def _tool_name(tool) -> str:
    """Return the tool's advertised name (falls back to the function name)."""
    name = getattr(tool, "tool_name", None)
    if name:
        return name
    return _underlying_function(tool).__name__


def _resolved_origin_types(annotation) -> set:
    """Flatten an annotation into the concrete origin types it is built from.

    ``list[str]`` -> {list, str}; ``dict`` -> {dict}; ``str | None`` -> {str, NoneType}.

    A union (``str | None`` / ``typing.Optional[str]`` / ``typing.Union[...]``) is a type
    *constructor*, not a leaf type, so the union origin itself is not added to the result;
    only the concrete member types it composes are checked (they are all agnostic here).
    """
    origin = typing.get_origin(annotation)
    if origin is None:
        return {annotation}
    resolved: set = set()
    # Do not treat a union origin (types.UnionType / typing.Union) as a concrete type; it
    # merely composes agnostic member types (e.g. ``str | None``).
    if origin not in (types.UnionType, typing.Union):
        resolved.add(origin)
    for arg in typing.get_args(annotation):
        resolved |= _resolved_origin_types(arg)
    return resolved


def _contains_forbidden_token(text: str) -> list:
    """Return the list of forbidden tokens present (case-insensitively) in ``text``."""
    lowered = text.lower()
    return [tok for tok in FORBIDDEN_TOKENS if tok in lowered]


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: _tool_name(t))
def test_tool_name_has_no_provider_specific_token(tool):
    """No tool name references a provider or transport (Req 9.1)."""
    name = _tool_name(tool)
    offending = _contains_forbidden_token(name)
    assert offending == [], f"tool name {name!r} references provider-specific token(s): {offending}"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: _tool_name(t))
def test_parameter_names_have_no_provider_specific_token(tool):
    """No parameter name references a provider or transport (Req 9.1)."""
    fn = _underlying_function(tool)
    sig = inspect.signature(fn)
    for param_name in sig.parameters:
        offending = _contains_forbidden_token(param_name)
        assert offending == [], (
            f"parameter {param_name!r} of {_tool_name(tool)!r} references " f"provider-specific token(s): {offending}"
        )


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: _tool_name(t))
def test_annotation_strings_have_no_provider_specific_token(tool):
    """No raw (stringised) parameter/return annotation names a provider type (Req 9.1)."""
    fn = _underlying_function(tool)
    for target, annotation in fn.__annotations__.items():
        annotation_text = annotation if isinstance(annotation, str) else str(annotation)
        offending = _contains_forbidden_token(annotation_text)
        assert offending == [], (
            f"annotation for {target!r} of {_tool_name(tool)!r} references "
            f"provider-specific token(s): {offending} (annotation={annotation_text!r})"
        )


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: _tool_name(t))
def test_resolved_annotations_are_only_agnostic_primitive_types(tool):
    """Every resolved parameter/return type is a primitive/agnostic type (Req 9.1)."""
    fn = _underlying_function(tool)
    # Resolve stringised annotations (from __future__ import annotations) to real types.
    hints = typing.get_type_hints(fn)
    assert "return" in hints, f"{_tool_name(tool)!r} must declare a return annotation"

    for target, annotation in hints.items():
        resolved = _resolved_origin_types(annotation)
        disallowed = resolved - ALLOWED_TYPES
        assert disallowed == set(), (
            f"{target!r} of {_tool_name(tool)!r} uses non-agnostic type(s): " f"{sorted(str(t) for t in disallowed)}"
        )


def test_module_exposes_exactly_the_two_agnostic_tools():
    """The connector tool surface is exactly the two provider-agnostic tools (Req 9.1)."""
    exported = {
        name
        for name in dir(connector_tools)
        if getattr(getattr(connector_tools, name), "tool_type", None) == "function"
    }
    assert exported == {"get_iac_file", "propose_infrastructure_change"}
