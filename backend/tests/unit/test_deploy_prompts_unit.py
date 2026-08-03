"""Unit tests for role-aware Bedrock managed prompt deployment."""

# Standard library
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

# Third-party packages
import pytest

# Local modules
from config.settings import ORCHESTRATOR_MODEL_ID, SPECIALIST_MODEL_ID

pytestmark = pytest.mark.unit


def _client(existing_id=None):
    client = MagicMock()
    summaries = [] if existing_id is None else [{"name": "game-agent-test", "id": existing_id}]
    client.list_prompts.return_value = {"promptSummaries": summaries}
    client.create_prompt.return_value = {"id": "new-prompt"}
    client.create_prompt_version.return_value = {"version": "1", "arn": "arn:prompt:1"}
    return client


@pytest.mark.parametrize(
    ("prompt_name", "expected_model"),
    [
        ("orchestrator", ORCHESTRATOR_MODEL_ID),
        ("gamelift_specialist", SPECIALIST_MODEL_ID),
        ("eks_specialist", SPECIALIST_MODEL_ID),
        ("cost_specialist", SPECIALIST_MODEL_ID),
    ],
)
def test_new_prompt_uses_agent_role_model(monkeypatch, prompt_name, expected_model):
    # Local modules
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name=prompt_name, text="prompt text", version="1")
    client = _client()
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")

    deploy_prompts.deploy_prompt(client, "unused", vp)

    variant = client.create_prompt.call_args.kwargs["variants"][0]
    assert variant["modelId"] == expected_model
    assert variant["templateConfiguration"]["text"]["text"] == "prompt text"


def test_model_only_change_updates_and_publishes(monkeypatch):
    # Local modules
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="eks_specialist", text="same text", version="2")
    client = _client(existing_id="existing")
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    client.get_prompt.return_value = {
        "variants": [
            {
                "name": "default",
                "modelId": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "templateType": "TEXT",
                "inferenceConfiguration": {"text": {"temperature": 0.1}},
                "templateConfiguration": {"text": {"text": "same text"}},
            }
        ]
    }

    deploy_prompts.deploy_prompt(client, "unused", vp)

    client.update_prompt.assert_called_once()
    assert client.update_prompt.call_args.kwargs["variants"][0]["modelId"] == SPECIALIST_MODEL_ID
    client.create_prompt_version.assert_called_once_with(promptIdentifier="existing")


def test_complete_variant_match_is_unchanged(monkeypatch):
    # Local modules
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="orchestrator", text="same text", version="2")
    client = _client(existing_id="existing")
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    client.get_prompt.return_value = {
        "variants": [
            {
                "name": "default",
                "modelId": ORCHESTRATOR_MODEL_ID,
                "templateType": "TEXT",
                "inferenceConfiguration": {"text": {"temperature": 0.0}},
                "templateConfiguration": {"text": {"text": "same text"}},
            }
        ]
    }
    client.list_prompts.side_effect = [
        {"promptSummaries": [{"name": "game-agent-test", "id": "existing"}]},
        {
            "promptSummaries": [{"version": "2", "arn": "arn:prompt:2"}],
            "nextToken": "page-2",
        },
        {
            "promptSummaries": [
                {"version": "DRAFT", "arn": "arn:prompt:draft"},
                {"version": "10", "arn": "arn:prompt:10"},
                {"version": "invalid", "arn": "arn:prompt:invalid"},
            ]
        },
    ]

    result = deploy_prompts.deploy_prompt(client, "unused", vp)

    assert result == "arn:prompt:10"
    assert client.list_prompts.call_args_list[2].kwargs == {
        "promptIdentifier": "existing",
        "nextToken": "page-2",
    }
    client.update_prompt.assert_not_called()
    client.create_prompt_version.assert_not_called()
