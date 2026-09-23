"""Scanner-verifiable YAML loading for repository-owned CloudFormation templates.

Static-analysis tools flag ``yaml.load`` even when it is handed a
``yaml.SafeLoader`` subclass, because they cannot follow the loader's
inheritance to prove the dangerous Python constructors are unavailable. This
module replaces that pattern with an explicit, scoped helper that a scanner can
verify by reading a single function:

* a dedicated :class:`yaml.SafeLoader` subclass is instantiated directly (no
  ``yaml.load`` indirection);
* the CloudFormation short-form intrinsic constructors are registered on the
  subclass only, so the global ``yaml.SafeLoader`` constructor registry is
  never mutated; and
* the loader is always disposed in a ``finally`` block.

The loader intentionally keeps every safety property of ``yaml.SafeLoader``:
Python object, module, and callable tags (``!!python/...``) are not resolvable
and fail closed. CloudFormation ``!`` tags are converted into plain strings,
lists, and mappings, which is all these contract tests inspect.
"""

# Standard library
from typing import Any

# Third-party packages
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


class CloudFormationSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` subclass that preserves CloudFormation intrinsic values.

    Custom constructors are registered on this subclass alone (see
    :func:`_construct_intrinsic` below), leaving the base ``yaml.SafeLoader``
    registry untouched.
    """


def _construct_intrinsic(loader: yaml.SafeLoader, _tag_suffix: str, node: yaml.Node) -> Any:
    """Convert a CloudFormation short-form ``!`` tag into plain Python data.

    Only the plain, non-executable node kinds are handled; anything else raises
    ``TypeError`` so unexpected shapes fail closed rather than silently
    dropping data.
    """
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"Unsupported CloudFormation YAML node: {type(node).__name__}")


# Register the intrinsic handler on the dedicated subclass only. add_multi_constructor
# is a classmethod; calling it on the subclass mutates the subclass registry and
# leaves yaml.SafeLoader's global registry unchanged.
CloudFormationSafeLoader.add_multi_constructor("!", _construct_intrinsic)


def load_cfn_template(text: str) -> Any:
    """Parse one CloudFormation template document with the scoped safe loader.

    Instantiates :class:`CloudFormationSafeLoader` directly, reads the single
    document via ``get_single_data()``, and disposes the loader in ``finally``.
    No ``yaml.load`` call is involved, which keeps the safety property visible
    to static analysis.
    """
    loader = CloudFormationSafeLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
