"""
Unit tests: memory facts shaped like Guardrail-anonymized values are never stored.

Retrieved long-term memory reaches the model outside the input Guardrail, so the
write path must refuse any fact carrying a value the Guardrail would anonymize
(AWS account IDs, internal hostnames, emails, IPv4 addresses and CIDRs, phone
numbers). All identifiers below are synthetic or documentation-range values.
"""

# Standard library
import re
from pathlib import Path
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_ACTOR = "actor-pii-shape-test"
_GUARDRAIL_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "infrastructure" / "cloudformation" / "04-bedrock-guardrails.yaml"
)

_SENSITIVE_VALUES = [
    "123456789012",
    "ip-10-0-0-1.ec2.internal",
    "player.one@example.com",
    "198.51.100.7",
    "198.51.100.0/24",
    "+1 206-555-0100",
]


def _saved_contents(mock_save) -> list:
    return [call.args[1] for call in mock_save.call_args_list]


def _logged_text(mock_logger) -> str:
    return "\n".join(str(call) for call in mock_logger.mock_calls)


@pytest.mark.parametrize("value", _SENSITIVE_VALUES)
def test_predicate_recognizes_each_anonymized_shape(value):
    # Local modules
    from utils.semantic_memory import contains_anonymized_value

    assert contains_anonymized_value(value)
    assert contains_anonymized_value(f"Game 'Mmo' runs on fleet: {value}")


@pytest.mark.parametrize(
    "text",
    [
        "Game 'Mmo' runs on cluster: agones-demo",
        "Game 'Mmo' runs on fleet: game-server-fleet",
        "User's name is Alex",
        "Instance type: c5.large",
        "Peak players: 10k (weekends)",
        "Concurrent players: 5000",
        "Session duration: 45 minutes",
    ],
)
def test_predicate_accepts_ordinary_facts(text):
    # Local modules
    from utils.semantic_memory import contains_anonymized_value

    assert not contains_anonymized_value(text)


@pytest.mark.parametrize(
    "message, fragment",
    [
        # The capture holds the whole value.
        ("Our game runs on fleet 123456789012", "123456789012"),
        ("Call me 123456789012", "123456789012"),
        ("Our game runs on fleet 206-555-0100", "206"),
        ("Call me 2065550100", "2065550100"),
        # The capture stops at a dot or @ and holds only part of the value; the
        # overlap with the value in the source message still rejects it.
        ("Our game runs on cluster ip-10-0-0-1.ec2.internal", "ip-10"),
        ("Our game runs on fleet player.one@example.com", "player"),
        ("Call me alex.one@example.com", "alex"),
        ("Our game runs on cluster 198.51.100.7", "198"),
        ("Our game runs on cluster 198.51.100.0/24", "198"),
        ("Our game runs on fleet 1-206-555-0100", "206"),
        # The name capture takes two words, "206 555", of a spaced phone number.
        ("Call me 206 555 0100", "206"),
    ],
)
def test_anonymized_shaped_fact_is_not_stored(message, fragment):
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    # Precondition: with the gate disabled, an extraction pattern does capture
    # the value, so the case below exercises the gate rather than a non-match.
    with (
        patch("utils.semantic_memory._fact_carries_anonymized_value", return_value=False),
        patch("utils.semantic_memory.save_semantic_memory", return_value=True) as ungated_save,
    ):
        extract_and_save_user_info(_ACTOR, message, "Noted!")
    assert any(fragment.lower() in content.lower() for content in _saved_contents(ungated_save))

    with (
        patch("utils.semantic_memory.save_semantic_memory", return_value=True) as mock_save,
        patch("utils.semantic_memory.logger") as mock_logger,
    ):
        extract_and_save_user_info(_ACTOR, message, "Noted!")

    assert not any(fragment.lower() in content.lower() for content in _saved_contents(mock_save))
    logged = _logged_text(mock_logger)
    assert fragment not in logged
    assert _ACTOR not in logged


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Our game runs on cluster agones-demo", "Game 'Game' runs on cluster: agones-demo"),
        ("My MMO runs on fleet game-server-fleet", "Game 'Mmo' runs on fleet: game-server-fleet"),
        ("My name is Alex", "User's name is Alex"),
    ],
)
def test_ordinary_facts_are_still_stored(message, expected):
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with (
        patch("utils.semantic_memory.save_semantic_memory", return_value=True) as mock_save,
        patch("utils.semantic_memory.logger") as mock_logger,
    ):
        extract_and_save_user_info(_ACTOR, message, "Noted!")

    assert expected in _saved_contents(mock_save)
    assert "Skipped" not in _logged_text(mock_logger)


def test_mixed_message_keeps_safe_fact_and_logs_only_a_count():
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    message = "My name is Alex and our game runs on fleet 123456789012 from 198.51.100.7"
    with (
        patch("utils.semantic_memory.save_semantic_memory", return_value=True) as mock_save,
        patch("utils.semantic_memory.logger") as mock_logger,
    ):
        extract_and_save_user_info(_ACTOR, message, "Noted!")

    saved = _saved_contents(mock_save)
    assert saved == ["User's name is Alex"]

    skip_lines = [
        call.args[0] for call in mock_logger.info.call_args_list if call.args and "Skipped" in str(call.args[0])
    ]
    assert len(skip_lines) == 1
    # The line is fixed text plus a count: no fact text, value, or actor.
    assert re.fullmatch(r"\S+ Skipped 1 memory fact\(s\) shaped like a Guardrail-anonymized value", skip_lines[0])
    logged = _logged_text(mock_logger)
    for forbidden in ("123456789012", "198.51.100", "Alex", _ACTOR, "fleet:"):
        assert forbidden not in logged


def test_every_anonymize_regex_in_the_guardrail_template_is_mirrored():
    """The template's ANONYMIZE regexes must appear verbatim in the write-time check."""
    # Local modules
    from utils.semantic_memory import _ANONYMIZED_VALUE_PATTERNS

    template = _GUARDRAIL_TEMPLATE.read_text(encoding="utf-8")
    anonymize_regexes = re.findall(r"Pattern:\s*'([^']+)'\s*\n\s*Action:\s*'ANONYMIZE'", template)
    assert anonymize_regexes, "expected ANONYMIZE regexes in the Guardrail template"

    mirrored = {pattern.pattern for pattern in _ANONYMIZED_VALUE_PATTERNS}
    for regex in anonymize_regexes:
        assert regex in mirrored
