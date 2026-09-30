"""Mocked integration coverage for specialist Knowledge Base projections."""

# Standard library
import json
from typing import Any
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

pytestmark = pytest.mark.integration


class _RetrieveClient:
    """Return one authoritative Bedrock Retrieve-shaped response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "retrievalResults": [
                {
                    "content": {"type": "TEXT", "text": "Use bounded target tracking guidance."},
                    "location": {
                        "type": "S3",
                        "s3Location": {"uri": "s3://synthetic-docs/private/path.md"},
                    },
                    "metadata": {
                        "title": "Scaling guidance",
                        "deployment-id": "synthetic-deployment-private",
                    },
                    "score": 0.9,
                }
            ]
        }


def _invoke(tool: Any, **kwargs: Any) -> str:
    function = getattr(tool, "__wrapped__", None) or getattr(tool, "_tool_func")
    return str(function(**kwargs))


@pytest.mark.parametrize(
    "knowledge_base_id",
    ["kb-gamelift-synthetic", "kb-eks-synthetic", "kb-cost-synthetic"],
)
def test_each_specialist_kb_factory_path_returns_only_the_projected_envelope(knowledge_base_id: str) -> None:
    """GameLift, EKS, and Cost use the same bounded provider boundary."""
    clear_kb_cache()
    client = _RetrieveClient()
    tool = create_kb_retrieve_tool(knowledge_base_id, "us-west-2")

    with patch("utils.kb_tools._build_agent_runtime_client", return_value=client):
        serialized = _invoke(tool, text="scaling", numberOfResults=1, score=0.5)

    envelope = json.loads(serialized)
    assert envelope["schemaVersion"] == "kb-projection-v1"
    assert envelope["state"] == "complete"
    assert envelope["returnedCount"] == 1
    assert client.calls[0]["knowledgeBaseId"] == knowledge_base_id
    assert "synthetic-deployment-private" not in serialized
    assert "synthetic-docs" not in serialized
