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

# An earlier-generation model ID that accepts the temperature parameter, used to
# exercise the temperature normalization and idempotency paths independently of
# whichever generation the role defaults currently resolve to.
LEGACY_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

# A current-generation model ID that rejects the temperature parameter, used to
# pin the temperature-omission path independently of the resolved role defaults.
NO_TEMPERATURE_MODEL_ID = "global.anthropic.claude-haiku-5-5"


def _pin_model(monkeypatch, deploy_prompts, agent_key, model_id):
    """Point one agent key at an explicit model so the test is env-independent."""
    config = dict(deploy_prompts.INFERENCE_CONFIG)
    config[agent_key] = {**config[agent_key], "model_id": model_id}
    monkeypatch.setattr(deploy_prompts, "INFERENCE_CONFIG", config)


def _use_legacy_model(monkeypatch, deploy_prompts, agent_key):
    """Point one agent key at a legacy model so a temperature is published."""
    _pin_model(monkeypatch, deploy_prompts, agent_key, LEGACY_MODEL_ID)


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
        ("source_control_specialist", SPECIALIST_MODEL_ID),
    ],
)
def test_new_prompt_uses_agent_role_model(monkeypatch, prompt_name, expected_model):
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name=prompt_name, text="prompt text", version="1")
    client = _client()
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")

    deploy_prompts.deploy_prompt(client, "unused", vp)

    variant = client.create_prompt.call_args.kwargs["variants"][0]
    assert variant["modelId"] == expected_model
    assert variant["templateConfiguration"]["text"]["text"] == "prompt text"


def test_source_control_prompt_registered_under_env_key():
    """The Source Control managed prompt is wired into the deploy mapping under
    GBAW_SOURCE_CONTROL_PROMPT_ARN, mirroring the cost prompt (PR #319 finding F4)."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    # Local modules
    from agents.optimized_prompts import SOURCE_CONTROL_PROMPT

    assert deploy_prompts.PROMPTS["GBAW_SOURCE_CONTROL_PROMPT_ARN"] is SOURCE_CONTROL_PROMPT


def test_new_prompt_omits_temperature_for_models_that_reject_it(monkeypatch):
    """A variant for a model that rejects temperature publishes none."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="orchestrator", text="prompt text", version="1")
    client = _client()
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    _pin_model(monkeypatch, deploy_prompts, "orchestrator", NO_TEMPERATURE_MODEL_ID)

    deploy_prompts.deploy_prompt(client, "unused", vp)

    variant = client.create_prompt.call_args.kwargs["variants"][0]
    # Claude Haiku 5.5 rejects temperature.
    assert variant["modelId"] == NO_TEMPERATURE_MODEL_ID
    assert deploy_prompts.get_model_capabilities(variant["modelId"]).send_temperature is False
    assert "temperature" not in variant["inferenceConfiguration"]["text"]


def test_new_prompt_keeps_temperature_for_models_that_accept_it(monkeypatch):
    """An earlier-generation model still records its configured temperature."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="gamelift_specialist", text="prompt text", version="1")
    client = _client()
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    _use_legacy_model(monkeypatch, deploy_prompts, "gamelift")

    deploy_prompts.deploy_prompt(client, "unused", vp)

    variant = client.create_prompt.call_args.kwargs["variants"][0]
    assert variant["modelId"] == LEGACY_MODEL_ID
    assert variant["inferenceConfiguration"]["text"]["temperature"] == pytest.approx(0.1)


def test_model_only_change_updates_and_publishes(monkeypatch):
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="eks_specialist", text="same text", version="2")
    client = _client(existing_id="existing")
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    # Pin the current specialist model to Claude Sonnet 5.5 so the only change
    # from the stored Sonnet 4.5 variant is the model, independent of the env.
    _pin_model(monkeypatch, deploy_prompts, "eks", "global.anthropic.claude-sonnet-5-5")
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
    assert client.update_prompt.call_args.kwargs["variants"][0]["modelId"] == "global.anthropic.claude-sonnet-5-5"
    client.create_prompt_version.assert_called_once_with(promptIdentifier="existing")


def test_complete_variant_match_is_unchanged(monkeypatch):
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="orchestrator", text="same text", version="2")
    client = _client(existing_id="existing")
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    # Pin the orchestrator model to Claude Haiku 5.5, which rejects temperature,
    # so a matching published variant records no temperature either. Pinning
    # keeps the assertion independent of the resolved role environment.
    _pin_model(monkeypatch, deploy_prompts, "orchestrator", NO_TEMPERATURE_MODEL_ID)
    client.get_prompt.return_value = {
        "variants": [
            {
                "name": "default",
                "modelId": NO_TEMPERATURE_MODEL_ID,
                "templateType": "TEXT",
                "inferenceConfiguration": {"text": {}},
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


def test_bedrock_float32_temperature_is_idempotent(monkeypatch):
    """Bedrock's float32 round-trip must not publish a duplicate version."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="gamelift_specialist", text="same text", version="2")
    client = _client(existing_id="existing")
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    _use_legacy_model(monkeypatch, deploy_prompts, "gamelift")
    client.get_prompt.return_value = {
        "variants": [
            {
                "name": "default",
                "modelId": LEGACY_MODEL_ID,
                "templateType": "TEXT",
                "inferenceConfiguration": {"text": {"temperature": 0.10000000149011612}},
                "templateConfiguration": {"text": {"text": "same text"}},
            }
        ]
    }
    client.list_prompts.side_effect = [
        {"promptSummaries": [{"name": "game-agent-test", "id": "existing"}]},
        {"promptSummaries": [{"version": "1", "arn": "arn:prompt:1"}]},
    ]

    result = deploy_prompts.deploy_prompt(client, "unused", vp)

    assert result == "arn:prompt:1"
    client.update_prompt.assert_not_called()
    client.create_prompt_version.assert_not_called()


def test_meaningful_temperature_change_updates_and_publishes(monkeypatch):
    """A real temperature change must survive normalization and publish a new version."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    vp = SimpleNamespace(name="gamelift_specialist", text="same text", version="2")
    client = _client(existing_id="existing")
    monkeypatch.setattr(deploy_prompts, "_prompt_resource_name", lambda unused: "game-agent-test")
    _use_legacy_model(monkeypatch, deploy_prompts, "gamelift")
    # Stored temperature (0.9) differs meaningfully from the gamelift config value (0.1);
    # model and text match so temperature is the only field driving the update.
    client.get_prompt.return_value = {
        "variants": [
            {
                "name": "default",
                "modelId": LEGACY_MODEL_ID,
                "templateType": "TEXT",
                "inferenceConfiguration": {"text": {"temperature": 0.9}},
                "templateConfiguration": {"text": {"text": "same text"}},
            }
        ]
    }

    deploy_prompts.deploy_prompt(client, "unused", vp)

    client.update_prompt.assert_called_once()
    assert client.update_prompt.call_args.kwargs["variants"][0]["inferenceConfiguration"]["text"][
        "temperature"
    ] == pytest.approx(0.1)
    client.create_prompt_version.assert_called_once_with(promptIdentifier="existing")


def test_normalize_temperature_boundaries():
    """_normalize_temperature absorbs float32 noise but keeps changes >= 1e-6."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    # None passes through unchanged (variant without an inference temperature).
    assert deploy_prompts._normalize_temperature(None) is None
    # Exact values are preserved.
    assert deploy_prompts._normalize_temperature(0.0) == 0.0
    assert deploy_prompts._normalize_temperature(0.1) == pytest.approx(0.1)
    # Bedrock's float32 round-trip of 0.1 collapses back to the config value.
    assert deploy_prompts._normalize_temperature(0.10000000149011612) == deploy_prompts._normalize_temperature(0.1)
    # Sub-microscopic noise is absorbed...
    assert deploy_prompts._normalize_temperature(0.1000001) == deploy_prompts._normalize_temperature(0.1)
    # ...but a change at the 1e-6 resolution is preserved.
    assert deploy_prompts._normalize_temperature(0.100001) != deploy_prompts._normalize_temperature(0.1)


def _run_main(monkeypatch, env_file, published):
    """Run deploy_prompts.main() with every prompt 'published' at the given ARNs."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    monkeypatch.setattr(deploy_prompts, "boto3", SimpleNamespace(client=lambda *args, **kwargs: MagicMock()))
    monkeypatch.setattr(deploy_prompts, "deploy_prompt", lambda client, env_key, vp: published[env_key])
    monkeypatch.setattr(sys, "argv", ["deploy_prompts.py", "--region", "us-west-2", "--env-file", str(env_file)])
    deploy_prompts.main()
    return deploy_prompts


def test_main_reports_that_changed_version_arns_need_a_runtime_update(monkeypatch, tmp_path, capsys):
    """Publishing never changes a running runtime: a changed ARN prints the update notice."""
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    keys = list(deploy_prompts.PROMPTS)
    published = {
        key: f"arn:aws:bedrock:us-west-2:123456789012:prompt/EXAMPLE{index}:2" for index, key in enumerate(keys)
    }
    env_file = tmp_path / ".env.local"
    # One key still records the previous version; the others were never recorded.
    env_file.write_text(f"{keys[0]}=arn:aws:bedrock:us-west-2:123456789012:prompt/EXAMPLE0:1\nUNRELATED=1\n")

    _run_main(monkeypatch, env_file, published)

    out = capsys.readouterr().out
    assert f"{len(keys)} prompt version ARN(s) changed" in out
    assert "take effect only after a runtime update" in out
    # The env file now records the published version ARNs and keeps other lines.
    assert deploy_prompts.read_env_values(str(env_file), keys) == published
    assert "UNRELATED=1" in env_file.read_text()


def test_main_prints_no_update_notice_when_recorded_arns_are_unchanged(monkeypatch, tmp_path, capsys):
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    published = {
        key: f"arn:aws:bedrock:us-west-2:123456789012:prompt/EXAMPLE{index}:2"
        for index, key in enumerate(deploy_prompts.PROMPTS)
    }
    env_file = tmp_path / ".env.local"
    env_file.write_text("".join(f"{key}={arn}\n" for key, arn in published.items()))

    _run_main(monkeypatch, env_file, published)

    assert "changed" not in capsys.readouterr().out


def test_changed_keys_compares_each_recorded_value():
    # Third-party packages
    from scripts.infrastructure import deploy_prompts

    assert deploy_prompts.changed_keys({"A": "1", "B": "2"}, {"A": "1", "B": "3", "C": "4"}) == ["B", "C"]
    assert deploy_prompts.changed_keys({"A": "1"}, {"A": "1"}) == []
