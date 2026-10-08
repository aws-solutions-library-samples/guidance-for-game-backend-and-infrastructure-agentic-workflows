"""Bounded, safe parsing of untrusted CloudFormation templates (YAML or JSON).

Templates handed to this module are model output and must be treated as
hostile input. Parsing therefore enforces explicit limits *before* and *during*
YAML composition, so a crafted document cannot exhaust the runtime:

* input size (bytes) is checked before parsing;
* exactly one document is accepted;
* anchors and aliases are rejected outright (no "billion laughs" expansion);
* node count, nesting depth, collection size, and scalar length are bounded
  while the node graph is composed;
* only core YAML types and the CloudFormation short-form intrinsic tags are
  accepted; any other tag (including ``!!python/...``) is rejected;
* duplicate mapping keys and malformed intrinsics are rejected rather than
  silently resolved.

Intrinsics are mapped to their long-form JSON shape (``{"Ref": ...}``,
``{"Fn::GetAtt": [...]}``) so callers can tell them apart from literal values.
The loader subclasses ``yaml.SafeLoader`` and registers its constructors on the
subclass only; no Python object construction is possible.
"""

# Standard library
from typing import Any

# Third-party packages
import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

MAX_TEMPLATE_BYTES = 51_200  # CloudFormation's TemplateBody limit
MAX_NODES = 20_000
MAX_DEPTH = 40
MAX_COLLECTION_ITEMS = 2_000
MAX_SCALAR_CHARS = 16_384

# CloudFormation short-form intrinsic functions and condition functions.
INTRINSIC_TAGS = frozenset(
    {
        "Ref",
        "Condition",
        "Base64",
        "Cidr",
        "FindInMap",
        "GetAtt",
        "GetAZs",
        "ImportValue",
        "Join",
        "Length",
        "Select",
        "Split",
        "Sub",
        "ToJsonString",
        "Transform",
        "And",
        "Equals",
        "If",
        "Not",
        "Or",
    }
)
_CORE_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null", "map", "seq", "timestamp")
)


class TemplateParseError(ValueError):
    """A template was rejected. ``code`` is a stable, code-owned reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _long_form_key(tag_suffix: str) -> str:
    # "!Ref" and "!Condition" keep their bare names; every other short form
    # ("!Sub", "!GetAtt", "!If", ...) is the "Fn::" long form.
    return tag_suffix if tag_suffix in {"Ref", "Condition"} else f"Fn::{tag_suffix}"


class BoundedCloudFormationLoader(yaml.SafeLoader):
    """``SafeLoader`` with resource limits, a tag allowlist, and strict mappings."""

    def __init__(self, stream: str):
        super().__init__(stream)
        self._node_count = 0
        self._depth = 0

    def compose_node(self, parent: Node | None, index: Any) -> Node:  # type: ignore[override]
        event = self.peek_event()
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
            raise TemplateParseError("aliases_not_allowed")
        if isinstance(event, ScalarEvent) and len(event.value) > MAX_SCALAR_CHARS:
            raise TemplateParseError("scalar_too_long")
        self._node_count += 1
        if self._node_count > MAX_NODES:
            raise TemplateParseError("too_many_nodes")
        if isinstance(event, CollectionStartEvent):
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise TemplateParseError("too_deep")
        try:
            node = super().compose_node(parent, index)
        finally:
            if isinstance(event, CollectionStartEvent):
                self._depth -= 1
        if isinstance(node, (SequenceNode, MappingNode)) and len(node.value) > MAX_COLLECTION_ITEMS:
            raise TemplateParseError("collection_too_large")
        tag = node.tag or ""
        if tag.startswith("!"):
            if tag[1:] not in INTRINSIC_TAGS:
                raise TemplateParseError("unsupported_tag")
        elif tag not in _CORE_TAGS:
            raise TemplateParseError("unsupported_tag")
        return node

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:  # type: ignore[override]
        if not isinstance(node, MappingNode):
            raise TemplateParseError("malformed_document")
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, ScalarNode):
                raise TemplateParseError("non_scalar_key")
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise TemplateParseError("duplicate_key")
            seen.add(key)
        mapping: dict[Any, Any] = super().construct_mapping(node, deep=deep)
        return mapping


def _construct_intrinsic(loader: BoundedCloudFormationLoader, tag_suffix: str, node: Node) -> Any:
    value: Any
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:  # pragma: no cover - the composer only produces the three node kinds
        raise TemplateParseError("malformed_intrinsic")

    if tag_suffix in {"Ref", "Condition"} and not (isinstance(value, str) and value):
        raise TemplateParseError("malformed_intrinsic")
    if tag_suffix == "GetAtt":
        if isinstance(value, str):
            resource, _, attribute = value.partition(".")
            if not resource or not attribute:
                raise TemplateParseError("malformed_intrinsic")
            value = [resource, attribute]
        elif not (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)):
            raise TemplateParseError("malformed_intrinsic")
    if tag_suffix == "Sub" and not (
        isinstance(value, str) or (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str))
    ):
        raise TemplateParseError("malformed_intrinsic")
    return {_long_form_key(tag_suffix): value}


BoundedCloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


def load_cfn_template(text: str) -> Any:
    """Parse one untrusted CloudFormation template; raise :class:`TemplateParseError`."""
    if not isinstance(text, str):
        raise TemplateParseError("not_text")
    if len(text.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateParseError("template_too_large")
    loader = BoundedCloudFormationLoader(text)
    try:
        return loader.get_single_data()
    except TemplateParseError:
        raise
    except ComposerError as exc:
        if "single document" in str(exc):
            raise TemplateParseError("multiple_documents") from None
        raise TemplateParseError("invalid_yaml") from None
    except ConstructorError:
        raise TemplateParseError("unsupported_tag") from None
    except yaml.YAMLError:
        raise TemplateParseError("invalid_yaml") from None
    except RecursionError:
        raise TemplateParseError("too_deep") from None
    finally:
        loader.dispose()


def is_intrinsic(value: Any) -> bool:
    """Whether ``value`` is a CloudFormation intrinsic function or reference."""
    if not isinstance(value, dict) or len(value) != 1:
        return False
    (key,) = value
    return isinstance(key, str) and (key in {"Ref", "Condition"} or key.startswith("Fn::"))
