"""Regression tests for knowledge-base CloudFormation templates."""

# Standard library
import pathlib

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE_DIR = PROJECT_ROOT / "infrastructure/cloudformation"
KNOWLEDGE_BASES = ("gamelift", "eks", "cost")
NON_FILTERABLE_KEYS = ["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]


def _load_template(knowledge_base):
    path = TEMPLATE_DIR / f"knowledge-base-{knowledge_base}.yaml"
    return load_cfn_template(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("knowledge_base", KNOWLEDGE_BASES)
def test_vector_index_replacement_uses_versioned_name_and_nonfilterable_metadata(knowledge_base):
    template = _load_template(knowledge_base)
    expected_index_name = f"${{ProjectName}}-{knowledge_base}-index-v2"

    index_properties = template["Resources"]["VectorIndex"]["Properties"]
    assert index_properties["IndexName"] == expected_index_name
    assert index_properties["MetadataConfiguration"]["NonFilterableMetadataKeys"] == NON_FILTERABLE_KEYS

    storage = template["Resources"]["KnowledgeBase"]["Properties"]["StorageConfiguration"]
    assert storage["S3VectorsConfiguration"]["IndexName"] == expected_index_name


@pytest.mark.parametrize("knowledge_base", KNOWLEDGE_BASES)
def test_knowledge_base_replacement_uses_versioned_name(knowledge_base):
    """The v2 index forces KB replacement (StorageConfiguration is create-only),
    so the account-unique KB name must be versioned too or create-before-delete
    fails with 409 AlreadyExists against the original KB."""
    template = _load_template(knowledge_base)

    kb_name = template["Resources"]["KnowledgeBase"]["Properties"]["Name"]
    assert kb_name == f"${{ProjectName}}-{knowledge_base}-kb-v2"


def test_gamelift_data_source_scopes_ingestion_to_document_prefix():
    """Non-document artifacts in the shared bucket must not enter KB ingestion."""
    template = _load_template("gamelift")
    s3_configuration = template["Resources"]["DataSource"]["Properties"]["DataSourceConfiguration"]["S3Configuration"]

    assert s3_configuration["InclusionPrefixes"] == ["gamelift/"]


def test_eks_chunk_size_is_restored_after_metadata_fix():
    template = _load_template("eks")
    ingestion = template["Resources"]["DataSource"]["Properties"]["VectorIngestionConfiguration"]
    fixed_size = ingestion["ChunkingConfiguration"]["FixedSizeChunkingConfiguration"]

    assert fixed_size["MaxTokens"] == 256
