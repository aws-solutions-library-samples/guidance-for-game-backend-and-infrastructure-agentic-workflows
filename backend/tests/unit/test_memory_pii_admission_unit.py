"""Recognizable sensitive values never enter long-term memory."""

# Standard library
import re
import time
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from utils import semantic_memory
from utils.security import MAX_PROMPT_LENGTH

pytestmark = pytest.mark.unit

_SENSITIVE_VALUES = [
    "000000000000",
    "player@example.com",
    "192.0.2.17",
    "2001:db8::17",
    "game-server.internal",
    "+1 (202) 555-0142",
    "2025550142",
]


@pytest.mark.parametrize("value", _SENSITIVE_VALUES)
def test_sensitive_fact_is_rejected_before_any_memory_client_call(value):
    client = MagicMock()
    with (
        patch.object(semantic_memory, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-synthetic"),
        patch.object(semantic_memory, "_get_memory_client", return_value=client) as get_client,
    ):
        saved = semantic_memory.save_semantic_memory("actor-synthetic", f"Game runs on {value}")

    assert saved is False
    get_client.assert_not_called()
    client.batch_create_memory_records.assert_not_called()


@pytest.mark.parametrize("value", _SENSITIVE_VALUES)
def test_sensitive_input_is_not_truncated_into_an_apparently_safe_fact(value):
    # The extraction patterns can stop before dots or punctuation. Check the
    # original message before extraction so an address is not saved as "192".
    with patch.object(semantic_memory, "save_semantic_memory", return_value=True) as save:
        semantic_memory.extract_and_save_user_info(
            "actor-synthetic", f"My name is Zephyr. Our game uses cluster-{value}.", "Noted."
        )

    save.assert_called_once_with("actor-synthetic", "User's name is Zephyr")


def test_ordinary_profile_and_game_facts_still_save():
    with patch.object(semantic_memory, "save_semantic_memory", return_value=True) as save:
        semantic_memory.extract_and_save_user_info(
            "actor-synthetic",
            "My name is Zephyr. I run a battle royale game. Our sessions last 40 minutes. "
            "Our game uses cluster-gameagent-test.",
            "Noted.",
        )

    facts = [call.args[1] for call in save.call_args_list]
    assert "User's name is Zephyr" in facts
    assert "Game type: BATTLE ROYALE" in facts
    assert "Session duration: 40 minutes" in facts
    assert "Game 'Game' runs on cluster: gameagent-test" in facts


def test_game_resource_identifiers_and_instance_sizes_are_not_contact_details():
    with patch.object(semantic_memory, "save_semantic_memory", return_value=True) as save:
        semantic_memory.extract_and_save_user_info(
            "actor-synthetic",
            "Our game runs on fleet-a1b2c3d4-e5f6-4789-abcd-ef0123456789. We use c5.2xlarge.",
            "Noted.",
        )

    assert any("fleet:" in call.args[1] for call in save.call_args_list)
    assert any("Instance type: c5.2xlarge" == call.args[1] for call in save.call_args_list)


@pytest.mark.parametrize("unit", ["a-", "ab-", "ip-1-"])
def test_admission_scan_stays_fast_on_a_maximum_length_hyphenated_prompt(unit):
    # The admission patterns scan the whole message on every turn that stores
    # memory. An unbounded label repetition restarts at every hyphen and scans to
    # the end of the run, which turns a prompt at the length cap into seconds of
    # CPU before the reply returns.
    prompt = (unit * (MAX_PROMPT_LENGTH // len(unit) + 1))[:MAX_PROMPT_LENGTH]
    with patch.object(semantic_memory, "save_semantic_memory", return_value=True):
        started = time.perf_counter()
        semantic_memory.extract_and_save_user_info("actor-synthetic", prompt, "Noted.")
        elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"admission scan took {elapsed:.2f}s on a {len(prompt)}-character prompt"


@pytest.mark.parametrize(
    "text",
    [
        "ip-10-0-1-5.ec2.internal",
        "my-host.internal",
        "game-server.INTERNAL",
        "a" * 63 + ".internal",
        "-".join(["segment"] * 12) + ".internal",
    ],
)
def test_internal_hostnames_still_match_the_bounded_label(text):
    assert semantic_memory.contains_anonymized_value(f"Our build host is {text} today")


def test_internal_hostname_check_matches_what_the_template_regex_matches_on_real_labels():
    template = re.compile(r"\b[a-z0-9-]+\.internal\b", re.IGNORECASE)
    samples = [
        "node-7.internal",
        "ip-172-31-5-9.us-west-2.compute.internal",
        "x.internal",
        "internal",
        ".internal",
        "host.internals",
        "host.internal-zone",
        "a" * 63 + ".internal",
    ]
    for sample in samples:
        expected = bool(template.search(sample))
        assert bool(semantic_memory._INTERNAL_HOSTNAME_PATTERN.search(sample)) == expected, sample
