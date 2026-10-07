"""Safe parsing of CloudFormation YAML/JSON templates.

CloudFormation short-form intrinsics (``!Ref``, ``!Sub``, ``!GetAtt``, ...) are
YAML tags that ``yaml.SafeLoader`` rejects. This loader maps each tag to its
long-form JSON shape (``{"Ref": ...}`` / ``{"Fn::Sub": ...}``) so callers can
recognize intrinsic values instead of mistaking them for literal data. Custom
constructors are registered on the subclass only; the global ``SafeLoader``
registry is untouched and no object construction is possible.
"""

# Standard library
from typing import Any

# Third-party packages
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


class CloudFormationSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` subclass that understands CloudFormation intrinsic tags."""


def _long_form_key(tag_suffix: str) -> str:
    # "!Ref" and "!Condition" keep their bare names; every other short form
    # ("!Sub", "!GetAtt", "!If", ...) is the "Fn::" long form.
    return tag_suffix if tag_suffix in {"Ref", "Condition"} else f"Fn::{tag_suffix}"


def _construct_intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    value: Any
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
        if tag_suffix == "GetAtt" and isinstance(value, str):
            # Short-form "!GetAtt Resource.Attribute" is a dotted string.
            value = value.split(".", 1)
    elif isinstance(node, SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        raise TypeError(f"Unsupported CloudFormation YAML node: {type(node).__name__}")
    return {_long_form_key(tag_suffix): value}


CloudFormationSafeLoader.add_multi_constructor("!", _construct_intrinsic)


def load_cfn_template(text: str) -> Any:
    """Parse one CloudFormation template document (YAML or JSON)."""
    loader = CloudFormationSafeLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def is_intrinsic(value: Any) -> bool:
    """Whether ``value`` is a CloudFormation intrinsic function or reference."""
    if not isinstance(value, dict) or len(value) != 1:
        return False
    (key,) = value
    return isinstance(key, str) and (key in {"Ref", "Condition"} or key.startswith("Fn::"))
