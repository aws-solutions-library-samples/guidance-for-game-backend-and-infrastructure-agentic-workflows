"""Regression tests for knowledge-base CloudFormation templates."""

# Standard library
import pathlib

# Third-party packages
import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE_DIR = PROJECT_ROOT / "infrastructure/cloudformation"
KNOWLEDGE_BASES = ("gamelift", "eks", "cost")
NON_FILTERABLE_KEYS = ["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]


class CloudFormationLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves CloudFormation intrinsic values."""


def _construct_intrinsic(loader, _tag_suffix, node):
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"Unsupported CloudFormation YAML node: {type(node).__name__}")


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


def _load_template(knowledge_base):
    path = TEMPLATE_DIR / f"knowledge-base-{knowledge_base}.yaml"
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)


@pytest.mark.parametrize("knowledge_base", KNOWLEDGE_BASES)
def test_vector_index_replacement_uses_versioned_name_and_nonfilterable_metadata(knowledge_base):
    template = _load_template(knowledge_base)
    expected_index_name = f"${{ProjectName}}-{knowledge_base}-index-v2"

    index_properties = template["Resources"]["VectorIndex"]["Properties"]
    assert index_properties["IndexName"] == expected_index_name
    assert index_properties["MetadataConfiguration"]["NonFilterableMetadataKeys"] == NON_FILTERABLE_KEYS

    storage = template["Resources"]["KnowledgeBase"]["Properties"]["StorageConfiguration"]
    assert storage["S3VectorsConfiguration"]["IndexName"] == expected_index_name


def test_eks_chunk_size_is_restored_after_metadata_fix():
    template = _load_template("eks")
    ingestion = template["Resources"]["DataSource"]["Properties"]["VectorIngestionConfiguration"]
    fixed_size = ingestion["ChunkingConfiguration"]["FixedSizeChunkingConfiguration"]

    assert fixed_size["MaxTokens"] == 256
